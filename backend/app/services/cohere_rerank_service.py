from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.core.settings import CohereSettings
from app.domain.models import SearchHit


class CohereRerankError(RuntimeError):
    pass


class CohereRerankService:
    def __init__(self, settings: CohereSettings) -> None:
        self._settings = settings

    def is_enabled(self) -> bool:
        return self._settings.is_configured()

    def rerank(
        self,
        query: str,
        hits: list[SearchHit],
        limit: int,
    ) -> list[SearchHit]:
        if not self.is_enabled() or not hits:
            return hits[:limit]

        candidate_window = min(len(hits), max(limit * 2, 24), 72)
        candidate_hits = hits[:candidate_window]
        documents = [self._document_text(hit) for hit in candidate_hits]
        payload = {
            "model": self._settings.model,
            "query": query,
            "documents": documents,
            "top_n": min(len(documents), max(limit, 12)),
        }
        response = self._request_json("POST", "/rerank", payload)
        results = response.get("results", [])
        if not results:
            raise CohereRerankError("Cohere returned no rerank results.")

        reranked: list[SearchHit] = []
        for result in results:
            index = int(result.get("index", -1))
            if index < 0 or index >= len(candidate_hits):
                continue

            hit = candidate_hits[index]
            relevance_score = float(result.get("relevance_score") or 0.0)
            hit.rerank_score = round(relevance_score, 4)
            hit.score = round((0.3 * hit.score) + (0.7 * relevance_score), 4)
            reranked.append(hit)

        if not reranked:
            raise CohereRerankError("Cohere returned only invalid rerank indices.")

        used_skus = {hit.product.sku for hit in reranked}
        for hit in candidate_hits:
            if hit.product.sku in used_skus:
                continue
            reranked.append(hit)

        for hit in hits[candidate_window:]:
            if hit.product.sku in used_skus:
                continue
            reranked.append(hit)

        reranked.sort(key=lambda hit: hit.score, reverse=True)
        return reranked[:limit]

    def _document_text(self, hit: SearchHit) -> str:
        product = hit.product
        attributes = ", ".join(
            f"{key}: {', '.join(values)}"
            for key, values in sorted(product.attributes.items())
            if values
        )
        return "\n".join(
            [
                f"title: {product.name}",
                f"brand: {product.brand}",
                f"categories: {', '.join(product.categories)}",
                f"description: {product.description}",
                f"attributes: {attributes}",
                f"in_stock: {'yes' if product.in_stock else 'no'}",
            ]
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
            raise CohereRerankError("Cohere is not configured.")

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
            raise CohereRerankError(
                f"Cohere request failed with HTTP {error.code}: {detail}"
            ) from error
        except URLError as error:
            raise CohereRerankError(f"Cohere request failed: {error.reason}") from error
