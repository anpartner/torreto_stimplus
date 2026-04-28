from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from app.core.settings import TypesenseSettings
from app.domain.models import Product, StructuredQuery
from app.domain.text import extract_first_number


class TypesenseError(RuntimeError):
    pass


@dataclass(slots=True)
class TypesenseSearchResult:
    products: list[Product]
    found: int


class TypesenseService:
    MAX_PER_PAGE = 250
    QUERY_BY = (
        "name,brand,categories,product_family,screen_size,storage,color,"
        "attribute_values,description,searchable_text,sku"
    )
    QUERY_BY_WEIGHTS = "12,8,6,7,7,4,3,2,1,1,10"
    INCLUDE_FIELDS = (
        "id,sku,name,description,brand,categories,attributes_json,price,currency,"
        "in_stock,popularity"
    )

    def __init__(self, settings: TypesenseSettings) -> None:
        self._settings = settings

    def is_enabled(self) -> bool:
        return self._settings.is_configured()

    def sync_products(self, products: list[Product]) -> int:
        if not self.is_enabled():
            return 0

        self._recreate_collection()
        return self.upsert_products(products)

    def upsert_products(self, products: list[Product]) -> int:
        if not self.is_enabled():
            return 0

        self._ensure_collection()
        indexed_count = 0
        for start in range(0, len(products), self._settings.import_batch_size):
            batch = [
                self._product_to_document(product)
                for product in products[start : start + self._settings.import_batch_size]
            ]
            self._import_documents(batch)
            indexed_count += len(batch)
        return indexed_count

    def reset_collection(self) -> None:
        if not self.is_enabled():
            return
        self._request_json(
            "DELETE",
            f"/collections/{self._settings.collection_name}",
            ignore_not_found=True,
        )

    def search(
        self,
        structured_query: StructuredQuery,
        limit: int,
        *,
        screen_size_window: int = 1,
        use_storage_filter: bool = True,
    ) -> TypesenseSearchResult:
        if not self.is_enabled():
            return TypesenseSearchResult(products=[], found=0)

        per_page = self._candidate_page_size(limit, has_filters=bool(structured_query.filters))
        payload = self._request_json(
            "GET",
            f"/collections/{self._settings.collection_name}/documents/search",
            params=self._search_params(
                structured_query,
                per_page,
                screen_size_window=screen_size_window,
                use_storage_filter=use_storage_filter,
            ),
        )
        hits = payload.get("hits", [])
        products = [self._document_to_product(hit.get("document", {})) for hit in hits]
        products = [product for product in products if product is not None]
        return TypesenseSearchResult(products=products, found=int(payload.get("found", 0)))

    def _candidate_page_size(self, limit: int, *, has_filters: bool) -> int:
        per_page = max(limit * self._settings.candidate_multiplier, limit, 10)
        if has_filters:
            per_page = max(per_page, 120)
        return min(per_page, self.MAX_PER_PAGE)

    def health(self) -> dict[str, Any]:
        if not self.is_enabled():
            return {"enabled": False}

        payload = self._request_json("GET", "/health")
        payload["enabled"] = True
        return payload

    def get_product_by_sku(self, sku: str) -> Product | None:
        if not self.is_enabled() or not sku:
            return None

        payload = self._request_json(
            "GET",
            f"/collections/{self._settings.collection_name}/documents/search",
            params={
                "q": sku,
                "query_by": "sku",
                "filter_by": self._facet_list_clause("sku", [sku]),
                "per_page": "1",
                "include_fields": self.INCLUDE_FIELDS,
                "prioritize_exact_match": "true",
            },
        )
        hits = payload.get("hits", [])
        if not hits:
            return None
        return self._document_to_product(hits[0].get("document", {}))

    def _recreate_collection(self) -> None:
        if self._collection_exists():
            self._request_json(
                "DELETE",
                f"/collections/{self._settings.collection_name}",
                ignore_not_found=True,
            )

        schema = {
            "name": self._settings.collection_name,
            "default_sorting_field": "popularity",
            "fields": [
                {"name": "sku", "type": "string", "facet": True, "infix": True},
                {"name": "name", "type": "string"},
                {"name": "description", "type": "string", "optional": True},
                {"name": "brand", "type": "string", "facet": True, "optional": True},
                {"name": "categories", "type": "string[]", "facet": True, "optional": True},
                {"name": "product_family", "type": "string[]", "facet": True, "optional": True},
                {"name": "screen_size", "type": "string[]", "facet": True, "optional": True},
                {"name": "storage", "type": "string[]", "facet": True, "optional": True},
                {"name": "color", "type": "string[]", "facet": True, "optional": True},
                {"name": "screen_size_bucket", "type": "int32[]", "facet": True, "optional": True},
                {
                    "name": "attribute_values",
                    "type": "string[]",
                    "optional": True,
                },
                {
                    "name": "searchable_text",
                    "type": "string",
                    "optional": True,
                },
                {"name": "price", "type": "float", "optional": True},
                {"name": "currency", "type": "string", "facet": True, "optional": True},
                {"name": "in_stock", "type": "bool", "facet": True, "optional": True},
                {"name": "popularity", "type": "float"},
                {
                    "name": "attributes_json",
                    "type": "string",
                    "optional": True,
                    "index": False,
                },
            ],
        }
        self._request_json("POST", "/collections", payload=schema)

    def _ensure_collection(self) -> None:
        if self._collection_exists():
            return
        self._recreate_collection()

    def _collection_exists(self) -> bool:
        try:
            self._request_json("GET", f"/collections/{self._settings.collection_name}")
            return True
        except TypesenseError as error:
            if "HTTP 404" in str(error):
                return False
            raise

    def _import_documents(self, documents: list[dict[str, Any]]) -> None:
        body = "\n".join(json.dumps(document, ensure_ascii=False) for document in documents)
        response = self._request_text(
            "POST",
            f"/collections/{self._settings.collection_name}/documents/import",
            params={
                "action": "upsert",
                "dirty_values": "coerce_or_drop",
            },
            body=body.encode("utf-8"),
            content_type="text/plain",
        )
        failed: list[str] = []
        for line in response.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not record.get("success"):
                failed.append(record.get("error", "unknown import error"))
        if failed:
            sample = "; ".join(failed[:3])
            raise TypesenseError(f"Typesense import failed for {len(failed)} documents: {sample}")

    def _product_to_document(self, product: Product) -> dict[str, Any]:
        attribute_values: list[str] = []
        for key, values in product.attributes.items():
            attribute_values.append(key)
            attribute_values.extend(values)

        deduped_values: list[str] = []
        for value in attribute_values:
            if value and value not in deduped_values:
                deduped_values.append(value)

        return {
            "id": product.source_id or product.sku,
            "sku": product.sku,
            "name": product.name,
            "description": product.description,
            "brand": product.brand,
            "categories": product.categories,
            "product_family": product.attributes.get("product_family", []),
            "screen_size": product.attributes.get("screen_size", []),
            "storage": product.attributes.get("storage", []),
            "color": product.attributes.get("color", []),
            "screen_size_bucket": self._screen_size_buckets(product.attributes.get("screen_size", [])),
            "attribute_values": deduped_values,
            "searchable_text": product.searchable_text(),
            "price": self._safe_float(product.price),
            "currency": product.currency,
            "in_stock": product.in_stock,
            "popularity": self._safe_float(product.popularity, default=0.0),
            "attributes_json": json.dumps(product.attributes, ensure_ascii=False),
        }

    def _safe_float(self, value: float | None, default: float | None = None) -> float | None:
        if value is None:
            return default
        normalized = float(value)
        if math.isfinite(normalized):
            return normalized
        return default

    def _screen_size_buckets(self, values: list[str]) -> list[int]:
        buckets: list[int] = []
        for value in values:
            size = extract_first_number(value)
            if size is None:
                continue
            bucket = int(math.floor(size + 0.5))
            if 1 <= bucket <= 200 and bucket not in buckets:
                buckets.append(bucket)
        return buckets

    def _search_params(
        self,
        structured_query: StructuredQuery,
        per_page: int,
        *,
        screen_size_window: int,
        use_storage_filter: bool,
    ) -> dict[str, str]:
        params = {
            "q": structured_query.normalized_text or structured_query.raw_query or "*",
            "query_by": self.QUERY_BY,
            "query_by_weights": self.QUERY_BY_WEIGHTS,
            "per_page": str(per_page),
            "include_fields": self.INCLUDE_FIELDS,
            "prioritize_exact_match": "true",
        }
        filter_by = self._build_filter_by(
            structured_query,
            screen_size_window=screen_size_window,
            use_storage_filter=use_storage_filter,
        )
        if filter_by:
            params["filter_by"] = filter_by
        return params

    def _build_filter_by(
        self,
        structured_query: StructuredQuery,
        *,
        screen_size_window: int,
        use_storage_filter: bool,
    ) -> str:
        clauses: list[str] = []

        brand_clause = self._facet_list_clause("brand", structured_query.filters.get("brand", []))
        if brand_clause:
            clauses.append(brand_clause)

        product_families = [
            value
            for value in structured_query.filters.get("product_family", [])
            if value.replace("_", "").isalnum()
        ]
        if product_families:
            clauses.append(f"product_family:=[{','.join(product_families)}]")

        color_clause = self._facet_list_clause("color", structured_query.filters.get("color", []))
        if color_clause:
            clauses.append(color_clause)

        storage_clause = self._facet_list_clause("storage", structured_query.filters.get("storage", []))
        if use_storage_filter and storage_clause:
            clauses.append(storage_clause)

        screen_size_buckets = self._screen_size_bucket_filter(
            structured_query.filters.get("screen_size", []),
            window=screen_size_window,
        )
        if screen_size_buckets:
            clauses.append(
                f"screen_size_bucket:=[{','.join(str(value) for value in screen_size_buckets)}]"
            )

        return " && ".join(clauses)

    def _facet_list_clause(self, field: str, values: list[str]) -> str:
        sanitized_values = [self._escape_filter_value(value) for value in values if value]
        if not sanitized_values:
            return ""
        return f"{field}:=[{','.join(sanitized_values)}]"

    def _escape_filter_value(self, value: str) -> str:
        escaped = value.replace("`", "'")
        return f"`{escaped}`"

    def _screen_size_bucket_filter(self, expected_values: list[str], *, window: int) -> list[int]:
        allowed: list[int] = []
        for expected in expected_values:
            size = extract_first_number(expected)
            if size is None:
                continue
            center = int(math.floor(size + 0.5))
            for candidate in range(center - window, center + window + 1):
                if 1 <= candidate <= 200 and candidate not in allowed:
                    allowed.append(candidate)
        return allowed

    def _document_to_product(self, document: dict[str, Any]) -> Product | None:
        sku = document.get("sku") or document.get("id")
        name = document.get("name")
        if not sku or not name:
            return None

        attributes_json = document.get("attributes_json") or "{}"
        try:
            attributes = json.loads(attributes_json)
        except json.JSONDecodeError:
            attributes = {}

        return Product(
            source_id=str(document.get("id") or sku),
            sku=str(sku),
            name=str(name),
            description=str(document.get("description") or ""),
            brand=str(document.get("brand") or "Unknown"),
            categories=[str(value) for value in document.get("categories") or []],
            attributes={
                str(key): [str(entry) for entry in values]
                for key, values in (attributes or {}).items()
                if isinstance(values, list)
            },
            price=float(document["price"]) if document.get("price") is not None else None,
            currency=str(document.get("currency") or "EUR"),
            in_stock=bool(document.get("in_stock", True)),
            popularity=float(document.get("popularity") or 0.0),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        *,
        ignore_not_found: bool = False,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        text = self._request_text(
            method,
            path,
            params=params,
            body=body,
            content_type="application/json" if payload is not None else None,
            ignore_not_found=ignore_not_found,
        )
        return json.loads(text) if text else {}

    def _request_text(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        *,
        ignore_not_found: bool = False,
    ) -> str:
        if not self._settings.url or not self._settings.api_key:
            raise TypesenseError("Typesense is not configured.")

        url = urljoin(self._settings.url.rstrip("/") + "/", path.lstrip("/"))
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {
            "X-TYPESENSE-API-KEY": self._settings.api_key,
        }
        if content_type:
            headers["Content-Type"] = content_type

        request = Request(url, data=body, headers=headers, method=method)

        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            if ignore_not_found and error.code == 404:
                return ""
            detail = error.read().decode("utf-8", errors="ignore")
            raise TypesenseError(f"Typesense request failed with HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise TypesenseError(f"Typesense request failed: {error.reason}") from error
