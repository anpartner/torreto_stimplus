from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Literal
from uuid import uuid4

from app.core.settings import Settings
from app.domain.models import Product, SearchHit, SearchResult, StructuredQuery
from app.services.catalog_ingestion import CatalogIngestionService, InMemoryCatalogStore
from app.services.catalog_state import SyncState
from app.services.cohere_rerank_service import CohereRerankService
from app.services.groq_query_service import GroqQueryService
from app.services.groq_query_service import CatalogFacetSnapshot
from app.services.hybrid_search import HybridSearchService, RerankerService
from app.services.query_understanding import PRODUCT_FAMILY_REWRITE_HINTS, QueryUnderstandingService
from app.services.typesense_service import TypesenseError, TypesenseService

IDENTIFIER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9-]+")


@dataclass(slots=True)
class SearchSession:
    session_id: str
    last_query: StructuredQuery | None = None
    history: list[str] = field(default_factory=list)


class SearchApplication:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog_store = InMemoryCatalogStore()
        self.ingestion_service = CatalogIngestionService(self.catalog_store, settings)
        self.query_understanding = QueryUnderstandingService(
            groq_query_service=GroqQueryService(settings.groq),
        )
        self.hybrid_search = HybridSearchService()
        self.reranker = RerankerService(
            cohere_rerank_service=CohereRerankService(settings.cohere),
        )
        self.typesense = TypesenseService(settings.typesense)
        self.catalog_source = str(settings.sample_catalog_path)
        self.catalog_mode = settings.catalog_mode
        self.typesense_indexed_products = 0
        self.sessions: dict[str, SearchSession] = {}
        self.catalog_facets = CatalogFacetSnapshot(brands=[], categories=[], attributes={})

    def bootstrap(self) -> None:
        sync_state = self.ingestion_service.get_sync_state()
        self.catalog_facets = self._catalog_facets_from_state(sync_state)

        if self.typesense.is_enabled() and sync_state.source == "akeneo" and sync_state.catalog_count:
            self.catalog_mode = "akeneo"
            self.catalog_source = self.settings.akeneo.base_url or "akeneo"
            self.typesense_indexed_products = sync_state.catalog_count
            return

        if self.ingestion_service.has_persisted_catalog():
            products = self.ingestion_service.load_persisted_catalog()
            self.catalog_facets = self.query_understanding._build_catalog_lookup(products).snapshot
            if self.typesense.is_enabled() and products:
                self.typesense_indexed_products = self.typesense.upsert_products(products)
            return

        self.reindex(source=self.settings.catalog_mode)

    def reindex(
        self,
        source: Literal["sample", "akeneo"] | None = None,
        source_path: str | Path | None = None,
        max_items: int | None = None,
        sync_mode: str = "full",
        reset_index: bool = False,
    ) -> int:
        if reset_index:
            self.reset_local_index()

        chosen_mode = (source or self.settings.catalog_mode or "sample").lower()

        if chosen_mode == "akeneo":
            sync_result = self.ingestion_service.sync_from_akeneo(
                sync_mode=sync_mode,
                max_products=max_items,
            )
            sync_state = self.ingestion_service.get_sync_state()
            self.catalog_mode = "akeneo"
            self.catalog_source = self.settings.akeneo.base_url or "akeneo"
            self.catalog_facets = self._catalog_facets_from_state(sync_state)
            if sync_result.sync_mode == "delta":
                indexed_delta = self.typesense.upsert_products(sync_result.products)
                self.typesense_indexed_products = max(
                    self.typesense_indexed_products,
                    indexed_delta,
                    sync_state.catalog_count,
                )
            else:
                self.typesense_indexed_products = self.typesense.sync_products(sync_result.products)
            self.sessions.clear()
            return sync_result.changed_products

        chosen_source = Path(source_path) if source_path else self.settings.sample_catalog_path
        products = self.ingestion_service.reindex_from_file(chosen_source)
        self.catalog_mode = "sample"
        self.catalog_source = str(chosen_source)
        self.catalog_facets = self.query_understanding._build_catalog_lookup(products).snapshot
        self.typesense_indexed_products = self.typesense.sync_products(products)
        self.sessions.clear()
        return len(products)

    def reset_local_index(self) -> None:
        self.ingestion_service.reset_local_catalog()
        self.catalog_source = ""
        self.typesense_indexed_products = 0
        self.sessions.clear()
        if self.typesense.is_enabled():
            self.typesense.reset_collection()

    def health(self) -> dict[str, str | int]:
        sync_state = self.ingestion_service.get_sync_state()
        typesense_status = "disabled"
        if self.typesense.is_enabled():
            try:
                self.typesense.health()
                typesense_status = "ok"
            except TypesenseError:
                typesense_status = "unreachable"

        return {
            "status": "ok",
            "products": max(self.catalog_store.count(), sync_state.catalog_count),
            "catalog_mode": self.catalog_mode,
            "catalog_source": str(self.catalog_source),
            "typesense_status": typesense_status,
            "typesense_indexed_products": max(self.typesense_indexed_products, sync_state.catalog_count),
            "last_sync_source": sync_state.source,
            "last_full_sync_at": sync_state.last_full_sync_at or "",
            "last_delta_sync_at": sync_state.last_delta_sync_at or "",
            "last_akeneo_updated_at": sync_state.last_akeneo_updated_at or "",
        }

    def list_products(self) -> list:
        return self.catalog_store.list()

    def get_product(self, sku: str) -> Product | None:
        product = self.catalog_store.get_by_sku(sku)
        if product is not None:
            return product
        if self.typesense.is_enabled():
            return self.typesense.get_product_by_sku(sku)
        return None

    def search(
        self,
        query: str,
        limit: int | None = None,
        session_id: str | None = None,
        visitor_id: str | None = None,
        reset_context: bool = False,
        previous_structured_query: StructuredQuery | None = None,
    ) -> SearchResult:
        products = self.catalog_store.list()
        active_session_id = session_id or uuid4().hex
        session_key = self._build_session_key(active_session_id, visitor_id)
        if reset_context:
            self.sessions.pop(session_key, None)

        session = self.sessions.setdefault(session_key, SearchSession(session_id=active_session_id))
        previous_query = None if reset_context else (previous_structured_query or session.last_query)
        structured_query = self.query_understanding.understand(
            query,
            products,
            previous_query=previous_query,
            facet_snapshot=self.catalog_facets,
        )
        exact_product = self._resolve_exact_identifier_product(query, structured_query)
        if exact_product is not None:
            exact_structured_query = StructuredQuery(
                raw_query=query,
                normalized_text=exact_product.sku.lower(),
                keywords=[exact_product.sku.lower()],
                filters={},
                boost_terms=[],
                intent="search",
                explanation=f"Reference exacte reconnue: sku={exact_product.sku}.",
                context_used=False,
                context_summary="",
            )
            exact_hit = SearchHit(
                product=exact_product,
                score=1.0,
                lexical_score=1.0,
                semantic_score=1.0,
                matched_terms=[exact_product.sku.lower()],
                matched_filters={},
            )
            session.last_query = exact_structured_query
            session.history.append(query)
            session.history = session.history[-8:]
            return SearchResult(
                query=query,
                structured_query=exact_structured_query,
                hits=[exact_hit],
                total_candidates=1,
                retrieval_backend="typesense" if self.typesense.is_enabled() else "memory",
                session_id=active_session_id,
                assistant_message=f"Reference exacte reconnue: {exact_product.sku}.",
                suggestion_chips=[],
            )
        target_limit = limit or self.settings.default_limit
        ranking_limit = min(max(target_limit * 2, 18), 72)
        if len(structured_query.filters.get("color", [])) > 1:
            ranking_limit = min(max(target_limit * 2, 24), 72)
        candidate_products = products
        total_candidates = len(products)
        retrieval_backend = "memory"
        relaxed_screen_size = False
        relaxed_storage = False

        if self.typesense.is_enabled():
            try:
                candidate_result = self.typesense.search(structured_query, target_limit)
                if (
                    not candidate_result.products
                    and structured_query.filters.get("storage")
                ):
                    candidate_result = self.typesense.search(
                        structured_query,
                        target_limit,
                        use_storage_filter=False,
                    )
                    relaxed_storage = bool(candidate_result.products)
                if (
                    not candidate_result.products
                    and structured_query.filters.get("screen_size")
                ):
                    candidate_result = self.typesense.search(
                        structured_query,
                        target_limit,
                        screen_size_window=2,
                    )
                    relaxed_screen_size = bool(candidate_result.products)
                candidate_products = candidate_result.products
                total_candidates = candidate_result.found
                retrieval_backend = (
                    "typesense-relaxed"
                    if relaxed_screen_size or relaxed_storage
                    else "typesense"
                )
                candidate_products = self._expand_multi_color_candidates(
                    structured_query,
                    candidate_products,
                    ranking_limit,
                )
                total_candidates = max(total_candidates, len(candidate_products))
            except TypesenseError:
                candidate_products = products
                total_candidates = len(products)
                retrieval_backend = "memory"

        hits = self.hybrid_search.search(structured_query, candidate_products, ranking_limit)
        reranked_hits = self.reranker.rerank(structured_query, hits, ranking_limit)
        reranked_hits = self._diversify_multi_value_filters(structured_query, reranked_hits, target_limit)
        assistant_message, suggestion_chips = self._build_assistant_guidance(
            query,
            structured_query,
            reranked_hits,
            products,
            relaxed_screen_size=relaxed_screen_size,
        )

        session.last_query = structured_query
        session.history.append(query)
        session.history = session.history[-8:]

        return SearchResult(
            query=query,
            structured_query=structured_query,
            hits=reranked_hits,
            total_candidates=total_candidates,
            retrieval_backend=retrieval_backend,
            session_id=active_session_id,
            assistant_message=assistant_message,
            suggestion_chips=suggestion_chips,
        )

    def _resolve_exact_identifier_product(
        self,
        query: str,
        structured_query: StructuredQuery,
    ) -> Product | None:
        candidates: list[str] = []

        for token in IDENTIFIER_TOKEN_PATTERN.findall(query or ""):
            self._append_identifier_candidate(candidates, token)

        for token in structured_query.keywords:
            self._append_identifier_candidate(candidates, token)

        for candidate in candidates:
            product = self.get_product(candidate)
            if product is not None:
                return product
        return None

    def _append_identifier_candidate(self, candidates: list[str], token: str) -> None:
        normalized = (token or "").strip()
        compact = normalized.replace("-", "")
        if not normalized or normalized.lower() == "sku":
            return
        if len(compact) < 5:
            return
        if not any(character.isalpha() for character in compact):
            return
        if not any(character.isdigit() for character in compact):
            return

        for candidate in (normalized, normalized.upper()):
            if candidate not in candidates:
                candidates.append(candidate)

    def _build_session_key(self, session_id: str, visitor_id: str | None) -> str:
        visitor_scope = visitor_id or "anonymous"
        return f"{visitor_scope}:{session_id}"

    def _catalog_facets_from_state(self, sync_state: SyncState) -> CatalogFacetSnapshot:
        payload = sync_state.catalog_facets or {}
        return CatalogFacetSnapshot(
            brands=[str(value) for value in payload.get("brands", [])],
            categories=[str(value) for value in payload.get("categories", [])],
            attributes={
                str(key): [str(entry) for entry in values]
                for key, values in payload.get("attributes", {}).items()
                if isinstance(values, list)
            },
        )

    def _build_assistant_guidance(
        self,
        query: str,
        structured_query: StructuredQuery,
        hits: list[SearchHit],
        products: list[Product],
        *,
        relaxed_screen_size: bool = False,
    ) -> tuple[str, list[str]]:
        if not hits:
            if structured_query.context_used:
                return (
                    "Aucun resultat avec le contexte memorise. Essaie un autre critere ou reinitialise la recherche.",
                    [],
                )
            if not structured_query.filters:
                return (
                    "Ajoute une marque, une categorie, une couleur ou une taille pour affiner la recherche.",
                    self._build_seed_suggestions(products),
                )
            return (
                "Aucun produit ne correspond a tous les criteres detectes. Reformule ou retire un critere.",
                self._build_relaxation_suggestions(structured_query),
            )

        if relaxed_screen_size:
            return (
                "Je n'ai pas trouve cette taille exacte. Je montre les PC les plus proches en taille.",
                self._build_refinement_suggestions(query, structured_query, hits),
            )

        if len(hits) > 3:
            return (
                "Je peux affiner cette recherche. Ajoute une marque, une couleur, une taille ou un stockage et je garderai le contexte.",
                self._build_refinement_suggestions(query, structured_query, hits),
            )

        if structured_query.context_used:
            return (
                "Recherche affinee avec la memoire de contexte active.",
                self._build_refinement_suggestions(query, structured_query, hits),
            )

        return (
            "Voici les resultats les plus pertinents pour cette requete.",
            self._build_refinement_suggestions(query, structured_query, hits),
        )

    def _build_seed_suggestions(self, products: list[Product]) -> list[str]:
        suggestions: list[str] = []
        seen_brands: list[str] = []
        seen_categories: list[str] = []

        for product in sorted(products, key=lambda item: item.popularity, reverse=True):
            if product.brand not in seen_brands:
                seen_brands.append(product.brand)
            for category in product.categories:
                if category not in seen_categories:
                    seen_categories.append(category)

        for brand in seen_brands[:2]:
            suggestions.append(brand.lower())
        for category in seen_categories[:2]:
            suggestions.append(category.lower())

        return suggestions[:4]

    def _build_relaxation_suggestions(self, structured_query: StructuredQuery) -> list[str]:
        suggestions: list[str] = []
        families = structured_query.filters.get("product_family", [])
        family_hint = PRODUCT_FAMILY_REWRITE_HINTS.get(families[0], "") if families else ""

        for key in ("brand", "storage", "color", "screen_size"):
            values = structured_query.filters.get(key, [])
            if not values:
                continue
            suggestion = self._compose_relaxed_query(
                family_hint=family_hint,
                value=values[0],
                fallback_query=structured_query.raw_query,
            )
            if suggestion and suggestion not in suggestions:
                suggestions.append(suggestion)

        if family_hint and family_hint not in suggestions:
            suggestions.append(family_hint)

        return suggestions[:4]

    def _compose_relaxed_query(
        self,
        *,
        family_hint: str,
        value: str,
        fallback_query: str,
    ) -> str:
        if family_hint and value:
            return f"{family_hint} {value}".strip()
        if family_hint:
            return family_hint
        if value:
            return value
        return fallback_query.strip()

    def _build_refinement_suggestions(
        self,
        query: str,
        structured_query: StructuredQuery,
        hits: list[SearchHit],
    ) -> list[str]:
        suggestions: list[str] = []

        if "brand" not in structured_query.filters:
            for brand in self._unique_values(hit.product.brand for hit in hits):
                suggestions.append(f"{query} {brand}".strip())
                if len(suggestions) >= 2:
                    break

        if "screen_size" not in structured_query.filters:
            for size in self._unique_values(
                value
                for hit in hits
                for value in hit.product.attributes.get("screen_size", [])
            ):
                suggestions.append(f"{query} {size}".strip())
                if len(suggestions) >= 3:
                    break

        if "color" not in structured_query.filters:
            for color in self._unique_values(
                value
                for hit in hits
                for value in hit.product.attributes.get("color", [])
            ):
                suggestions.append(f"{query} {color}".strip())
                if len(suggestions) >= 4:
                    break

        return suggestions[:4]

    def _unique_values(self, values: Iterable[str]) -> list[str]:
        unique: list[str] = []
        for value in values:
            if value and value not in unique:
                unique.append(value)
        return unique

    def _diversify_multi_value_filters(
        self,
        structured_query: StructuredQuery,
        hits: list[SearchHit],
        limit: int,
    ) -> list[SearchHit]:
        diversified_hits = hits
        for key in ("color",):
            requested_values = structured_query.filters.get(key, [])
            if len(requested_values) <= 1:
                continue
            diversified_hits = self._interleave_hits_by_filter_value(
                diversified_hits,
                key=key,
                requested_values=requested_values,
            )
        return diversified_hits[:limit]

    def _interleave_hits_by_filter_value(
        self,
        hits: list[SearchHit],
        *,
        key: str,
        requested_values: list[str],
    ) -> list[SearchHit]:
        buckets: dict[str, list[SearchHit]] = {value: [] for value in requested_values}
        unmatched: list[SearchHit] = []

        for hit in hits:
            matched_values = hit.matched_filters.get(key, [])
            assigned = False
            for requested_value in requested_values:
                if requested_value in matched_values:
                    buckets[requested_value].append(hit)
                    assigned = True
                    break
            if not assigned:
                unmatched.append(hit)

        interleaved: list[SearchHit] = []
        seen_skus: set[str] = set()
        progress = True

        while progress:
            progress = False
            for requested_value in requested_values:
                bucket = buckets[requested_value]
                while bucket:
                    candidate = bucket.pop(0)
                    if candidate.product.sku in seen_skus:
                        continue
                    interleaved.append(candidate)
                    seen_skus.add(candidate.product.sku)
                    progress = True
                    break

        for hit in [*unmatched, *hits]:
            if hit.product.sku in seen_skus:
                continue
            interleaved.append(hit)
            seen_skus.add(hit.product.sku)

        return interleaved

    def _expand_multi_color_candidates(
        self,
        structured_query: StructuredQuery,
        candidate_products: list[Product],
        limit: int,
    ) -> list[Product]:
        color_values = structured_query.filters.get("color", [])
        if len(color_values) <= 1 or not self.typesense.is_enabled():
            return candidate_products

        merged_products = {product.source_id: product for product in candidate_products}
        for color_value in color_values:
            color_variant = replace(
                structured_query,
                normalized_text=f"{structured_query.normalized_text} {color_value}".strip(),
                filters={
                    **structured_query.filters,
                    "color": [color_value],
                },
            )
            try:
                variant_result = self.typesense.search(color_variant, max(limit, 6))
            except TypesenseError:
                continue
            for product in variant_result.products:
                merged_products[product.source_id] = product

        return list(merged_products.values())


def build_search_application(settings: Settings) -> SearchApplication:
    return SearchApplication(settings)
