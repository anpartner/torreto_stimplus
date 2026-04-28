from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.core.settings import GroqSettings


class GroqQueryError(RuntimeError):
    pass


@dataclass(slots=True)
class CatalogFacetSnapshot:
    brands: list[str]
    categories: list[str]
    attributes: dict[str, list[str]]


@dataclass(slots=True)
class LlmStructuredQuery:
    intent: str
    use_previous_context: bool
    rewritten_query: str
    keywords: list[str]
    boost_terms: list[str]
    explanation: str
    clarification_message: str | None
    filters: dict[str, list[str]]


class GroqQueryService:
    def __init__(self, settings: GroqSettings) -> None:
        self._settings = settings

    def is_enabled(self) -> bool:
        return self._settings.is_configured()

    def parse_query(
        self,
        raw_query: str,
        previous_query_summary: str | None,
        snapshot: CatalogFacetSnapshot,
    ) -> LlmStructuredQuery:
        if not self.is_enabled():
            raise GroqQueryError("Groq is not configured.")

        payload = {
            "model": self._settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You convert ecommerce search queries into structured JSON for a search engine. "
                        "Use previous context only when the latest user message is clearly a refinement. "
                        "Use only brands, categories, colors, screen sizes, storage values, or product families that are present in the catalog hints. "
                        "If a value is unknown, return an empty list for that field. "
                        "Interpret ambiguous ecommerce language into the most likely product intent. "
                        "For generic words like pc or ordinateur, infer product_family=computer unless the user explicitly asks for a monitor, screen, accessory, software, or service. "
                        "Only infer narrower families like laptop, desktop, or all_in_one when the wording clearly points there. "
                        "Keep explanations short and factual. "
                        "Respond with one JSON object only, with exactly these top-level keys: "
                        "intent, use_previous_context, rewritten_query, keywords, boost_terms, explanation, clarification_message, filters. "
                        "The filters object must contain exactly these keys: brand, categories, color, screen_size, storage, product_family."
                    ),
                },
                {
                    "role": "user",
                    "content": self._build_user_prompt(
                        raw_query=raw_query,
                        previous_query_summary=previous_query_summary,
                        snapshot=snapshot,
                    ),
                },
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        response = self._request_json(
            "POST",
            "/chat/completions",
            payload=payload,
        )
        choices = response.get("choices", [])
        if not choices:
            raise GroqQueryError("Groq returned no choices.")

        content = choices[0].get("message", {}).get("content")
        if not content:
            raise GroqQueryError("Groq returned an empty message.")

        data = json.loads(content)
        filters = {
            key: [str(value) for value in values]
            for key, values in data.get("filters", {}).items()
            if isinstance(values, list)
        }
        return LlmStructuredQuery(
            intent=str(data.get("intent") or "search"),
            use_previous_context=bool(data.get("use_previous_context", False)),
            rewritten_query=str(data.get("rewritten_query") or ""),
            keywords=[str(value) for value in data.get("keywords", [])],
            boost_terms=[str(value) for value in data.get("boost_terms", [])],
            explanation=str(data.get("explanation") or ""),
            clarification_message=(
                str(data.get("clarification_message"))
                if data.get("clarification_message") is not None
                else None
            ),
            filters=filters,
        )

    def _build_user_prompt(
        self,
        raw_query: str,
        previous_query_summary: str | None,
        snapshot: CatalogFacetSnapshot,
    ) -> str:
        return json.dumps(
            {
                "user_query": raw_query,
                "previous_context": previous_query_summary or "",
                "catalog_hints": {
                    "brands": snapshot.brands,
                    "categories": snapshot.categories,
                    "attributes": snapshot.attributes,
                },
                "instructions": {
                    "keywords_limit": 8,
                    "boost_terms_limit": 6,
                    "follow_up_rule": (
                        "Set use_previous_context to true only if the query is an obvious refinement such as "
                        "'20 pouces', 'bleu', 'avec 1 To', 'plutot Dell', or similar."
                    ),
                    "product_family_rule": (
                        "When the query says pc or ordinateur without another explicit family, prefer product_family=computer. "
                        "Do not switch to monitor unless the user says ecran or moniteur."
                    ),
                },
            },
            ensure_ascii=False,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        text = self._request_text(
            method=method,
            path=path,
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        return json.loads(text) if text else {}

    def _request_text(
        self,
        method: str,
        path: str,
        body: bytes,
        content_type: str,
    ) -> str:
        if not self._settings.api_key:
            raise GroqQueryError("Groq is not configured.")

        url = urljoin(self._settings.base_url.rstrip("/") + "/", path.lstrip("/"))
        request = Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            method=method,
        )

        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore")
            raise GroqQueryError(
                f"Groq request failed with HTTP {error.code}: {detail}"
            ) from error
        except URLError as error:
            raise GroqQueryError(f"Groq request failed: {error.reason}") from error
