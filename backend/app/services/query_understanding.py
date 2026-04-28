from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from app.domain.models import Product, StructuredQuery
from app.domain.text import (
    extract_first_number,
    extract_sizes,
    extract_storage_values,
    normalize_text,
    tokenize,
)
from app.services.groq_query_service import (
    CatalogFacetSnapshot,
    GroqQueryError,
    GroqQueryService,
    LlmStructuredQuery,
)

SYNONYM_MAP = {
    "pc": ["ordinateur", "ordinateur portable", "laptop", "tout en un", "desktop"],
    "portable": ["ordinateur portable", "laptop"],
    "tv": ["televiseur", "television", "smart tv"],
    "telephone": ["smartphone", "mobile"],
    "smartphone": ["telephone", "mobile"],
    "iphone": ["smartphone", "apple"],
    "ecouteurs": ["earbuds", "audio", "bluetooth"],
}
PRODUCT_FAMILY_SYNONYMS = {
    "computer": ["pc", "ordinateur", "computer", "machine"],
    "laptop": ["ordinateur portable", "portable", "laptop", "notebook"],
    "desktop": ["ordinateur de bureau", "bureau", "desktop", "tour", "mini pc"],
    "all_in_one": ["tout en un", "all in one"],
    "monitor": ["ecran", "moniteur", "monitor", "display"],
    "smartphone": ["telephone", "smartphone", "mobile", "iphone"],
    "tablet": ["tablette", "tablet"],
}
PRODUCT_FAMILY_REWRITE_HINTS = {
    "computer": "ordinateur",
    "laptop": "ordinateur portable",
    "desktop": "ordinateur de bureau",
    "all_in_one": "ordinateur tout en un",
    "monitor": "ecran",
    "smartphone": "smartphone",
    "tablet": "tablette",
}
COMMON_COLOR_VALUES = {
    "noir": "Noir",
    "blanc": "Blanc",
    "bleu": "Bleu",
    "rouge": "Rouge",
    "vert": "Vert",
    "gris": "Gris",
    "argent": "Argent",
    "or": "Or",
    "rose": "Rose",
    "violet": "Violet",
    "jaune": "Jaune",
    "beige": "Beige",
    "marron": "Marron",
}

REFINEMENT_PREFIXES = (
    "avec ",
    "en ",
    "sans ",
    "mais ",
    "plutot ",
    "version ",
    "couleur ",
    "taille ",
)
REFINEMENT_MARKERS = {
    "avec",
    "sans",
    "mais",
    "plutot",
    "version",
    "couleur",
    "taille",
    "go",
    "gb",
    "tb",
    "to",
    "ssd",
}
COMPUTER_SPEC_MARKERS = {"ssd", "nvme", "ram", "ddr", "rtx", "geforce", "ryzen", "core"}
OVERRIDABLE_FILTERS = {"brand", "categories", "color", "screen_size", "storage", "product_family"}


@dataclass(slots=True)
class CatalogLookup:
    brands: dict[str, str]
    categories: dict[str, str]
    attribute_values: dict[str, dict[str, str]]
    snapshot: CatalogFacetSnapshot


class QueryUnderstandingService:
    def __init__(self, groq_query_service: GroqQueryService | None = None) -> None:
        self._groq_query_service = groq_query_service

    def understand(
        self,
        raw_query: str,
        products: list[Product],
        previous_query: StructuredQuery | None = None,
        facet_snapshot: CatalogFacetSnapshot | None = None,
    ) -> StructuredQuery:
        lookup = self._build_catalog_lookup(products, facet_snapshot=facet_snapshot)
        normalized_query = normalize_text(raw_query)
        heuristic_keywords = tokenize(raw_query)
        heuristic_filters = self._extract_filters(normalized_query, lookup)
        llm_parse = self._try_llm_parse(raw_query, previous_query, lookup)

        direct_keywords = llm_parse.keywords if llm_parse and llm_parse.keywords else heuristic_keywords
        direct_keywords = self._normalize_keywords(direct_keywords)
        direct_filters = (
            self._sanitize_filters(llm_parse.filters, lookup, heuristic_filters)
            if llm_parse
            else heuristic_filters
        )
        direct_filters = self._apply_spec_family_inference(normalized_query, direct_filters)
        heuristic_context = self._should_apply_context(
            normalized_query,
            direct_keywords,
            direct_filters,
            previous_query,
        )
        context_used = heuristic_context or (
            llm_parse.use_previous_context
            if llm_parse is not None
            else False
        )
        keywords = direct_keywords
        filters = direct_filters

        if context_used and previous_query is not None:
            filters = self._merge_filters(previous_query.filters, direct_filters)
            keywords = self._merge_keywords_with_overrides(
                previous_keywords=previous_query.keywords,
                current_keywords=direct_keywords,
                previous_filters=previous_query.filters,
                current_filters=direct_filters,
            )

        normalized_text = self._build_retrieval_text(
            raw_query=normalized_query,
            keywords=keywords,
            filters=filters,
        )
        normalized_text = self._strip_multi_value_color_terms(normalized_text, filters)
        normalized_text = self._rewrite_from_filters(normalized_text, filters)

        boost_terms = (
            llm_parse.boost_terms
            if llm_parse is not None and llm_parse.boost_terms
            else self._expand_terms(keywords)
        )
        boost_terms = self._merge_keywords(
            boost_terms,
            self._boost_terms_from_filters(filters),
        )
        context_summary = self._build_context_summary(filters, keywords) if context_used else ""
        explanation = self._build_explanation(
            filters,
            boost_terms,
            context_used,
            context_summary,
            llm_parse.explanation if llm_parse is not None else None,
        )

        return StructuredQuery(
            raw_query=raw_query,
            normalized_text=normalized_text,
            keywords=keywords,
            filters=filters,
            boost_terms=boost_terms,
            intent=llm_parse.intent if llm_parse is not None else "search",
            explanation=explanation,
            context_used=context_used,
            context_summary=context_summary,
        )

    def _normalize_keywords(self, candidates: list[str]) -> list[str]:
        normalized_keywords: list[str] = []
        for candidate in candidates:
            for token in tokenize(candidate):
                if token not in normalized_keywords:
                    normalized_keywords.append(token)
        return normalized_keywords

    def _build_catalog_lookup(
        self,
        products: list[Product],
        facet_snapshot: CatalogFacetSnapshot | None = None,
    ) -> CatalogLookup:
        if not products and facet_snapshot is not None:
            attribute_values = {
                key: {normalize_text(value): value for value in values if value}
                for key, values in facet_snapshot.attributes.items()
            }
            attribute_values.setdefault(
                "product_family",
                {normalize_text(value): value for value in PRODUCT_FAMILY_SYNONYMS},
            )
            return CatalogLookup(
                brands={normalize_text(value): value for value in facet_snapshot.brands if value},
                categories={normalize_text(value): value for value in facet_snapshot.categories if value},
                attribute_values=attribute_values,
                snapshot=facet_snapshot,
            )

        brands: dict[str, str] = {}
        categories: dict[str, str] = {}
        attribute_values: dict[str, dict[str, str]] = defaultdict(dict)
        brand_counts: Counter[str] = Counter()
        category_counts: Counter[str] = Counter()
        attribute_counts: dict[str, Counter[str]] = defaultdict(Counter)

        for product in products:
            normalized_brand = normalize_text(product.brand)
            if normalized_brand:
                brands[normalized_brand] = product.brand
                brand_counts[product.brand] += 1

            for category in product.categories:
                normalized_category = normalize_text(category)
                if normalized_category:
                    categories[normalized_category] = category
                    category_counts[category] += 1

            for key, values in product.attributes.items():
                for value in values:
                    normalized_value = normalize_text(value)
                    if normalized_value:
                        attribute_values[key][normalized_value] = value
                        attribute_counts[key][value] += 1

        snapshot = CatalogFacetSnapshot(
            brands=[value for value, _ in brand_counts.most_common(60)],
            categories=[value for value, _ in category_counts.most_common(60)],
            attributes={
                key: [value for value, _ in counter.most_common(30)]
                for key, counter in attribute_counts.items()
                if key in {"color", "storage", "screen_size", "product_family"}
            },
        )
        return CatalogLookup(
            brands=brands,
            categories=categories,
            attribute_values=dict(attribute_values),
            snapshot=snapshot,
        )

    def _try_llm_parse(
        self,
        raw_query: str,
        previous_query: StructuredQuery | None,
        lookup: CatalogLookup,
    ) -> LlmStructuredQuery | None:
        if self._groq_query_service is None or not self._groq_query_service.is_enabled():
            return None

        try:
            return self._groq_query_service.parse_query(
                raw_query=raw_query,
                previous_query_summary=previous_query.context_summary if previous_query else None,
                snapshot=lookup.snapshot,
            )
        except GroqQueryError:
            return None

    def _should_apply_context(
        self,
        normalized_query: str,
        keywords: list[str],
        filters: dict[str, list[str]],
        previous_query: StructuredQuery | None,
    ) -> bool:
        if previous_query is None or not previous_query.raw_query.strip():
            return False

        if not normalized_query.strip():
            return False

        if normalized_query.startswith(REFINEMENT_PREFIXES):
            return True

        if any(keyword in REFINEMENT_MARKERS for keyword in keywords) and len(keywords) <= 4:
            return True

        if len(keywords) <= 6 and filters and self._is_attribute_only_refinement(filters):
            return True

        if len(keywords) <= 4 and filters and self._is_single_slot_refinement(filters):
            return True

        if (
            len(keywords) <= 4
            and any(keyword.isdigit() for keyword in keywords)
            and not any(key in filters for key in {"brand", "categories", "product_family"})
        ):
            return True

        return False

    def _merge_keywords(
        self,
        previous_keywords: list[str],
        current_keywords: list[str],
    ) -> list[str]:
        merged: list[str] = []
        for keyword in [*previous_keywords, *current_keywords]:
            if keyword and keyword not in merged:
                merged.append(keyword)
        return merged

    def _merge_keywords_with_overrides(
        self,
        *,
        previous_keywords: list[str],
        current_keywords: list[str],
        previous_filters: dict[str, list[str]],
        current_filters: dict[str, list[str]],
    ) -> list[str]:
        suppressed_tokens: set[str] = set()

        for key, values in current_filters.items():
            if key not in OVERRIDABLE_FILTERS or not values:
                continue
            for previous_value in previous_filters.get(key, []):
                suppressed_tokens.update(tokenize(previous_value))

        preserved_previous = [
            keyword for keyword in previous_keywords if keyword not in suppressed_tokens
        ]
        return self._merge_keywords(preserved_previous, current_keywords)

    def _merge_filters(
        self,
        previous_filters: dict[str, list[str]],
        current_filters: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        merged = {
            key: list(values)
            for key, values in previous_filters.items()
        }

        for key, values in current_filters.items():
            if key in OVERRIDABLE_FILTERS:
                merged[key] = list(values)
                continue

            existing = merged.setdefault(key, [])
            for value in values:
                if value not in existing:
                    existing.append(value)

        return merged

    def _build_context_summary(
        self,
        filters: dict[str, list[str]],
        keywords: list[str],
    ) -> str:
        filter_fragments = []
        for key, values in filters.items():
            if not values:
                continue
            display_values = [
                PRODUCT_FAMILY_REWRITE_HINTS.get(value, value).replace("_", " ")
                for value in values
            ]
            filter_fragments.append(", ".join(display_values))
        if filter_fragments:
            return " | ".join(filter_fragments[:3])

        if keywords:
            return ", ".join(keywords[:4])

        return ""

    def _extract_filters(
        self,
        normalized_query: str,
        lookup: CatalogLookup,
    ) -> dict[str, list[str]]:
        filters: dict[str, list[str]] = defaultdict(list)

        storage_values = extract_storage_values(normalized_query)
        if len(storage_values) > 1 and any(
            self._contains_normalized_phrase(normalized_query, marker)
            for marker in COMPUTER_SPEC_MARKERS
        ):
            numeric_storage_values = sorted(
                storage_values,
                key=lambda value: extract_first_number(value) or 0.0,
                reverse=True,
            )
            storage_values = numeric_storage_values[:1]

        for storage_value in storage_values:
            filters["storage"].append(storage_value)

        for size in extract_sizes(normalized_query):
            filters["screen_size"].append(size)

        for color_key, color_value in COMMON_COLOR_VALUES.items():
            if self._contains_normalized_phrase(normalized_query, color_key):
                filters["color"].append(color_value)

        for family, family_terms in PRODUCT_FAMILY_SYNONYMS.items():
            if any(self._contains_normalized_phrase(normalized_query, normalize_text(term)) for term in family_terms):
                filters["product_family"].append(family)

        for brand_key, brand_value in lookup.brands.items():
            if brand_key and self._contains_normalized_phrase(normalized_query, brand_key):
                filters["brand"].append(brand_value)

        for category_key, category_value in lookup.categories.items():
            if category_key and self._contains_normalized_phrase(normalized_query, category_key):
                filters["categories"].append(category_value)

        for key in ("color", "storage", "screen_size", "product_family"):
            for candidate_key, candidate_value in lookup.attribute_values.get(key, {}).items():
                if candidate_key and self._contains_normalized_phrase(normalized_query, candidate_key):
                    filters[key].append(candidate_value)

        return {
            key: sorted(set(values), key=values.index)
            for key, values in filters.items()
            if values
        }

    def _sanitize_filters(
        self,
        candidate_filters: dict[str, list[str]],
        lookup: CatalogLookup,
        fallback_filters: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        sanitized: dict[str, list[str]] = {}

        for key, values in candidate_filters.items():
            if not values:
                continue

            if key == "categories" and key not in fallback_filters:
                continue

            if key == "brand":
                mapping = lookup.brands
            elif key == "categories":
                mapping = lookup.categories
            else:
                mapping = lookup.attribute_values.get(key, {})

            local_values: list[str] = []
            for value in values:
                canonical_value = mapping.get(normalize_text(value))
                if canonical_value and canonical_value not in local_values:
                    local_values.append(canonical_value)

            if local_values:
                sanitized[key] = local_values

        for key, values in fallback_filters.items():
            if key not in sanitized and values:
                sanitized[key] = list(values)

        return sanitized

    def _build_retrieval_text(
        self,
        *,
        raw_query: str,
        keywords: list[str],
        filters: dict[str, list[str]],
    ) -> str:
        fragments: list[str] = []

        for brand in filters.get("brand", []):
            fragments.append(brand)

        for family in filters.get("product_family", []):
            family_hint = PRODUCT_FAMILY_REWRITE_HINTS.get(family, family.replace("_", " "))
            fragments.append(family_hint)

        for keyword in keywords:
            fragments.append(keyword)

        for key in ("screen_size", "storage", "color"):
            for value in filters.get(key, []):
                fragments.append(value)

        deduped: list[str] = []
        seen: set[str] = set()
        for fragment in fragments:
            normalized_fragment = normalize_text(fragment)
            if not normalized_fragment or normalized_fragment in seen:
                continue
            deduped.append(normalized_fragment)
            seen.add(normalized_fragment)

        if deduped:
            return " ".join(deduped)

        return raw_query.strip()

    def _apply_spec_family_inference(
        self,
        normalized_query: str,
        filters: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        if filters.get("product_family"):
            return filters

        numbers = extract_storage_values(normalized_query)
        has_computer_marker = any(
            self._contains_normalized_phrase(normalized_query, marker)
            for marker in COMPUTER_SPEC_MARKERS
        )
        if has_computer_marker and numbers:
            enriched = {
                key: list(values)
                for key, values in filters.items()
            }
            enriched["product_family"] = ["computer"]
            return enriched

        return filters

    def _expand_terms(self, keywords: list[str]) -> list[str]:
        expanded: list[str] = []
        for keyword in keywords:
            expanded.extend(SYNONYM_MAP.get(keyword, []))
        return sorted(set(expanded))

    def _rewrite_from_filters(self, normalized_text: str, filters: dict[str, list[str]]) -> str:
        text = normalized_text.strip()
        families = filters.get("product_family", [])
        if not families:
            return text

        primary_family = families[0]
        family_hint = PRODUCT_FAMILY_REWRITE_HINTS.get(primary_family, "")
        if not family_hint:
            return text

        normalized_hint = normalize_text(family_hint)
        if normalized_hint and normalized_hint not in normalize_text(text):
            return f"{family_hint} {text}".strip()

        return text

    def _strip_multi_value_color_terms(self, normalized_text: str, filters: dict[str, list[str]]) -> str:
        colors = filters.get("color", [])
        if len(colors) <= 1:
            return normalized_text.strip()

        removable_tokens = {"ou"}
        for value in colors:
            removable_tokens.update(tokenize(value))

        kept_tokens = [
            token
            for token in tokenize(normalized_text)
            if token not in removable_tokens
        ]
        return " ".join(kept_tokens).strip() or normalized_text.strip()

    def _boost_terms_from_filters(self, filters: dict[str, list[str]]) -> list[str]:
        boosts: list[str] = []
        for family in filters.get("product_family", []):
            hint = PRODUCT_FAMILY_REWRITE_HINTS.get(family)
            if hint:
                boosts.append(hint)
            boosts.extend(PRODUCT_FAMILY_SYNONYMS.get(family, []))
        return boosts

    def _contains_normalized_phrase(self, normalized_query: str, normalized_phrase: str) -> bool:
        if not normalized_query or not normalized_phrase:
            return False
        haystack = f" {normalized_query.strip()} "
        needle = f" {normalized_phrase.strip()} "
        return needle in haystack

    def _build_explanation(
        self,
        filters: dict[str, list[str]],
        boost_terms: list[str],
        context_used: bool,
        context_summary: str,
        llm_explanation: str | None = None,
    ) -> str:
        fragments: list[str] = []

        if llm_explanation:
            fragments.append(llm_explanation.strip())

        if context_used and context_summary:
            fragments.append(f"Contexte memorise applique: {context_summary}.")

        if filters:
            filter_description = ", ".join(
                f"{key}={', '.join(values)}" for key, values in filters.items()
            )
            fragments.append(f"Filtres detectes: {filter_description}.")

        if boost_terms:
            fragments.append(
                "Expansion semantique: "
                + ", ".join(boost_terms[:5])
                + ("." if len(boost_terms) <= 5 else ", ...")
            )

        if not fragments:
            fragments.append(
                "Aucun filtre strict detecte, la recherche s'appuie sur le contexte global."
            )

        return " ".join(fragments)

    def _is_attribute_only_refinement(self, filters: dict[str, list[str]]) -> bool:
        active_keys = {key for key, values in filters.items() if values}
        if not active_keys:
            return False
        return active_keys <= {"color", "screen_size", "storage"}

    def _is_single_slot_refinement(self, filters: dict[str, list[str]]) -> bool:
        active_keys = {key for key, values in filters.items() if values}
        if len(active_keys) != 1:
            return False

        return active_keys <= {"brand", "categories", "product_family"}
