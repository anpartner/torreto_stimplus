from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from app.core.settings import AkeneoSettings


class AkeneoApiError(RuntimeError):
    pass


@dataclass(slots=True)
class AttributeMetadata:
    code: str
    type: str
    labels: dict[str, str] = field(default_factory=dict)
    localizable: bool = False
    scopable: bool = False

    def best_label(
        self,
        preferred_locale: str = "fr_FR",
        fallback_locale: str = "en_US",
    ) -> str:
        return (
            self.labels.get(preferred_locale)
            or self.labels.get(fallback_locale)
            or next(iter(self.labels.values()), self.code)
        )


class AkeneoApiClient:
    def __init__(self, settings: AkeneoSettings) -> None:
        self._settings = settings
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    def export_catalog(
        self,
        *,
        max_products: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, AttributeMetadata], dict[str, str]]:
        attributes = self.list_attributes()
        categories = self.list_categories()
        products = self.list_products(max_products=max_products)
        return products, attributes, categories

    def export_catalog_delta(
        self,
        *,
        updated_after: str,
        max_products: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, AttributeMetadata], dict[str, str]]:
        attributes = self.list_attributes()
        categories = self.list_categories()
        products = self.list_products_updated_since(updated_after, max_products=max_products)
        return products, attributes, categories

    def list_products(self, max_products: int | None = None) -> list[dict[str, Any]]:
        return list(
            self._iter_collection(
                "/api/rest/v1/products-uuid",
                params={
                    "limit": str(self._settings.page_limit),
                    "pagination_type": "search_after",
                    "with_attribute_options": "true",
                },
                max_items=max_products,
            )
        )

    def iter_product_batches(
        self,
        *,
        max_products: int | None = None,
    ):
        yield from self._iter_collection_pages(
            "/api/rest/v1/products-uuid",
            params={
                "limit": str(self._settings.page_limit),
                "pagination_type": "search_after",
                "with_attribute_options": "true",
            },
            max_items=max_products,
        )

    def list_products_updated_since(
        self,
        updated_after: str,
        max_products: int | None = None,
    ) -> list[dict[str, Any]]:
        search_payload = {
            "updated": [
                {
                    "operator": ">",
                    "value": self._format_updated_filter(updated_after),
                }
            ]
        }
        return list(
            self._iter_collection(
                "/api/rest/v1/products-uuid",
                params={
                    "limit": str(self._settings.page_limit),
                    "pagination_type": "search_after",
                    "with_attribute_options": "true",
                    "search": json.dumps(search_payload),
                },
                max_items=max_products,
            )
        )

    def list_attributes(self) -> dict[str, AttributeMetadata]:
        metadata: dict[str, AttributeMetadata] = {}
        for item in self._iter_collection(
            "/api/rest/v1/attributes",
            params={"limit": str(self._settings.page_limit)},
        ):
            metadata[item["code"]] = AttributeMetadata(
                code=item["code"],
                type=item.get("type", ""),
                labels=item.get("labels") or {},
                localizable=bool(item.get("localizable")),
                scopable=bool(item.get("scopable")),
            )
        return metadata

    def list_categories(self) -> dict[str, str]:
        categories: dict[str, str] = {}
        for item in self._iter_collection(
            "/api/rest/v1/categories",
            params={"limit": str(self._settings.page_limit)},
        ):
            labels = item.get("labels") or {}
            categories[item["code"]] = (
                labels.get(self._settings.preferred_locale)
                or labels.get(self._settings.fallback_locale)
                or next(iter(labels.values()), item["code"])
            )
        return categories

    def _iter_collection(
        self,
        path: str,
        params: dict[str, str] | None = None,
        max_items: int | None = None,
    ):
        url = self._build_url(path, params=params)
        yielded = 0

        while url:
            data = self._request_json("GET", url)
            for item in data.get("_embedded", {}).get("items", []):
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            url = data.get("_links", {}).get("next", {}).get("href")

    def _iter_collection_pages(
        self,
        path: str,
        params: dict[str, str] | None = None,
        max_items: int | None = None,
    ):
        url = self._build_url(path, params=params)
        yielded = 0

        while url:
            data = self._request_json("GET", url)
            items = list(data.get("_embedded", {}).get("items", []))
            if not items:
                return

            if max_items is not None:
                remaining = max_items - yielded
                if remaining <= 0:
                    return
                items = items[:remaining]

            yielded += len(items)
            yield items

            if max_items is not None and yielded >= max_items:
                return

            url = data.get("_links", {}).get("next", {}).get("href")

    def _request_json(
        self,
        method: str,
        url_or_path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
        retrying: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
        }
        body = None

        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")

        if authenticated:
            headers["Authorization"] = f"Bearer {self._get_access_token()}"
        elif self._settings.client_id and self._settings.client_secret:
            headers["Authorization"] = self._basic_authorization_header()

        request = Request(
            self._normalize_url(url_or_path),
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if authenticated and error.code == 401 and not retrying:
                self._access_token = None
                return self._request_json(
                    method,
                    url_or_path,
                    payload,
                    authenticated=authenticated,
                    retrying=True,
                )
            detail = error.read().decode("utf-8", errors="ignore")
            raise AkeneoApiError(
                f"Akeneo API request failed with HTTP {error.code}: {detail}"
            ) from error
        except URLError as error:
            raise AkeneoApiError(f"Akeneo API request failed: {error.reason}") from error

    def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token

        if not self._settings.is_configured():
            raise AkeneoApiError(
                "Akeneo credentials are missing. Set AKENEO_BASE_URL, "
                "AKENEO_USERNAME, AKENEO_PASSWORD, AKENEO_CLIENT_ID and "
                "AKENEO_CLIENT_SECRET."
            )

        payload = {
            "grant_type": "password",
            "username": self._settings.username,
            "password": self._settings.password,
        }
        response = self._request_json(
            "POST",
            "/api/oauth/v1/token",
            payload,
            authenticated=False,
        )
        self._access_token = response["access_token"]
        self._refresh_token = response.get("refresh_token")
        return self._access_token

    def _basic_authorization_header(self) -> str:
        raw = f"{self._settings.client_id}:{self._settings.client_secret}".encode("utf-8")
        return f"Basic {base64.b64encode(raw).decode('ascii')}"

    def _build_url(self, path: str, params: dict[str, str] | None = None) -> str:
        url = self._normalize_url(path)
        if not params:
            return url
        return f"{url}?{urlencode(params)}"

    def _normalize_url(self, url_or_path: str) -> str:
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            return url_or_path

        if not self._settings.base_url:
            raise AkeneoApiError("AKENEO_BASE_URL is missing.")

        return urljoin(self._settings.base_url.rstrip("/") + "/", url_or_path.lstrip("/"))

    def _format_updated_filter(self, value: str) -> str:
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value
