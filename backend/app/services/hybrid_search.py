from __future__ import annotations

from app.services.cohere_rerank_service import CohereRerankError, CohereRerankService
from app.domain.models import Product, SearchHit, StructuredQuery
from app.domain.text import (
    char_trigrams,
    extract_first_number,
    jaccard_similarity,
    normalize_text,
    tokenize,
)


class HybridSearchService:
    MIN_SEMANTIC_SCORE = 0.05
    HARD_FILTERS = {"brand", "categories", "color", "screen_size", "storage", "product_family"}
    ACCESSORY_MARKERS = {
        "clavier",
        "claviers",
        "keyboard",
        "dock",
        "docking",
        "station d accueil",
        "station d'accueil",
        "adaptateur",
        "adaptateurs",
        "cable",
        "cables",
        "souris",
        "mouse",
        "housse",
        "coque",
        "support",
        "batterie",
        "chargeur",
        "replacement",
        "remplacement",
    }
    PRODUCT_FAMILY_EQUIVALENTS = {
        "computer": {"computer", "desktop", "laptop", "all_in_one"},
        "desktop": {"desktop"},
        "laptop": {"laptop"},
        "all_in_one": {"all_in_one"},
        "monitor": {"monitor"},
        "smartphone": {"smartphone"},
        "tablet": {"tablet"},
    }

    def search(
        self,
        structured_query: StructuredQuery,
        products: list[Product],
        limit: int,
    ) -> list[SearchHit]:
        hits = [
            hit
            for product in products
            for hit in [self._score_product(structured_query, product)]
            if self._is_relevant(hit)
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(limit, 10)]

    def _score_product(self, structured_query: StructuredQuery, product: Product) -> SearchHit:
        product_text = product.searchable_text()
        product_tokens = set(tokenize(product_text))
        query_tokens = set(structured_query.keywords)
        boost_tokens = {
            token for term in structured_query.boost_terms for token in tokenize(term)
        }

        lexical_overlap = len(query_tokens & product_tokens) / max(len(query_tokens), 1)
        boost_overlap = len(boost_tokens & product_tokens) / max(len(boost_tokens), 1)
        exact_phrase_bonus = 0.25 if structured_query.normalized_text in normalize_text(product_text) else 0.0
        lexical_score = min(1.5, lexical_overlap + (0.5 * boost_overlap) + exact_phrase_bonus)

        semantic_score = max(
            jaccard_similarity(
                char_trigrams(structured_query.normalized_text),
                char_trigrams(product_text),
            ),
            (0.6 * lexical_overlap) + (0.4 * boost_overlap),
        )

        filter_score, matched_filters = self._filter_score(structured_query, product)
        business_score = (0.1 if product.in_stock else -0.1) + min(product.popularity, 100) / 500

        score = (
            (0.5 * lexical_score)
            + (0.3 * semantic_score)
            + (0.15 * filter_score)
            + (0.05 * business_score)
        )

        missing_hard_filters = self._missing_hard_filters(structured_query, matched_filters, product)
        if self._looks_like_accessory_for_computer_query(structured_query, product):
            missing_hard_filters = True
        if structured_query.filters and not matched_filters:
            score -= 0.15
        if missing_hard_filters:
            score = min(score - 0.35, -0.01)

        matched_terms = sorted((query_tokens | boost_tokens) & product_tokens)
        return SearchHit(
            product=product,
            score=round(score, 4),
            lexical_score=round(lexical_score, 4),
            semantic_score=round(semantic_score, 4),
            matched_terms=matched_terms,
            matched_filters=matched_filters,
        )

    def _is_relevant(self, hit: SearchHit) -> bool:
        if hit.score < 0:
            return False
        if hit.matched_filters:
            return True
        if hit.lexical_score > 0:
            return True
        if hit.semantic_score >= self.MIN_SEMANTIC_SCORE:
            return True
        return False

    def _filter_score(
        self,
        structured_query: StructuredQuery,
        product: Product,
    ) -> tuple[float, dict[str, list[str]]]:
        if not structured_query.filters:
            return 0.2, {}

        matched_filters: dict[str, list[str]] = {}
        total_expected = 0
        matched = 0

        for key, expected_values in structured_query.filters.items():
            total_expected += len(expected_values)
            if key == "brand":
                actual_values = [product.brand]
            elif key == "categories":
                actual_values = product.categories
            else:
                actual_values = product.attributes.get(key, [])

            normalized_actual = {normalize_text(value): value for value in actual_values}
            local_matches = []

            for expected in expected_values:
                normalized_expected = normalize_text(expected)
                if key == "product_family":
                    matched_value = self._match_product_family(expected, actual_values)
                    if matched_value:
                        local_matches.append(matched_value)
                        matched += 1
                    continue
                if key == "screen_size":
                    matched_value = self._match_screen_size(expected, actual_values)
                    if matched_value:
                        local_matches.append(matched_value)
                        matched += 1
                    continue
                if key == "color":
                    matched_value = self._match_loose_attribute(expected, actual_values)
                    if matched_value:
                        local_matches.append(matched_value)
                        matched += 1
                    continue

                if normalized_expected in normalized_actual:
                    local_matches.append(normalized_actual[normalized_expected])
                    matched += 1

            if local_matches:
                matched_filters[key] = local_matches

        return matched / max(total_expected, 1), matched_filters

    def _match_screen_size(self, expected: str, actual_values: list[str]) -> str | None:
        expected_size = extract_first_number(expected)
        if expected_size is None:
            return None

        for actual_value in actual_values:
            actual_size = extract_first_number(actual_value)
            if actual_size is None:
                continue
            if abs(actual_size - expected_size) <= 2.0:
                return actual_value

        return None

    def _match_product_family(self, expected: str, actual_values: list[str]) -> str | None:
        normalized_expected = normalize_text(expected)
        if not normalized_expected:
            return None

        allowed_values = self.PRODUCT_FAMILY_EQUIVALENTS.get(
            normalized_expected,
            {normalized_expected},
        )
        for actual_value in actual_values:
            if normalize_text(actual_value) in allowed_values:
                return actual_value
        return None

    def _match_loose_attribute(self, expected: str, actual_values: list[str]) -> str | None:
        normalized_expected = normalize_text(expected)
        if not normalized_expected:
            return None

        expected_tokens = set(normalized_expected.split())
        for actual_value in actual_values:
            normalized_actual = normalize_text(actual_value)
            if not normalized_actual:
                continue
            if normalized_expected in normalized_actual or normalized_actual in normalized_expected:
                return actual_value
            if expected_tokens and expected_tokens <= set(normalized_actual.split()):
                return actual_value
        return None

    def _missing_hard_filters(
        self,
        structured_query: StructuredQuery,
        matched_filters: dict[str, list[str]],
        product: Product,
    ) -> bool:
        for key, values in structured_query.filters.items():
            if key not in self.HARD_FILTERS or not values:
                continue

            if key == "brand":
                actual_values = [product.brand]
            elif key == "categories":
                actual_values = product.categories
            else:
                actual_values = product.attributes.get(key, [])

            if key in {"color", "storage", "screen_size"} and not actual_values:
                continue

            if key not in matched_filters:
                return True
        return False

    def _looks_like_accessory_for_computer_query(
        self,
        structured_query: StructuredQuery,
        product: Product,
    ) -> bool:
        requested_families = set(structured_query.filters.get("product_family", []))
        if not requested_families & {"computer", "laptop", "desktop", "all_in_one"}:
            return False

        haystack = normalize_text(" ".join([product.name, " ".join(product.categories)]))
        if not haystack:
            return False

        return any(marker in haystack for marker in self.ACCESSORY_MARKERS)


class RerankerService:
    def __init__(self, cohere_rerank_service: CohereRerankService | None = None) -> None:
        self._cohere_rerank_service = cohere_rerank_service

    def rerank(
        self,
        structured_query: StructuredQuery,
        hits: list[SearchHit],
        limit: int,
    ) -> list[SearchHit]:
        if self._cohere_rerank_service is not None and self._cohere_rerank_service.is_enabled():
            try:
                return self._cohere_rerank_service.rerank(
                    query=self._build_rerank_query(structured_query),
                    hits=hits,
                    limit=limit,
                )
            except CohereRerankError:
                pass

        reranked: list[SearchHit] = []

        for hit in hits:
            bonus = 0.0

            if structured_query.normalized_text in normalize_text(hit.product.name):
                bonus += 0.15
            if hit.matched_filters:
                bonus += 0.05 * len(hit.matched_filters)
            if hit.product.in_stock:
                bonus += 0.05

            hit.rerank_score = round(bonus, 4)
            hit.score = round(hit.score + bonus, 4)
            reranked.append(hit)

        reranked.sort(key=lambda hit: hit.score, reverse=True)
        return reranked[:limit]

    def _build_rerank_query(self, structured_query: StructuredQuery) -> str:
        query_parts = [structured_query.normalized_text or structured_query.raw_query]
        for key in ("brand", "product_family", "color", "screen_size", "storage"):
            values = structured_query.filters.get(key, [])
            if not values:
                continue
            query_parts.append(f"{key} {' '.join(values)}")
        return " | ".join(part for part in query_parts if part)
