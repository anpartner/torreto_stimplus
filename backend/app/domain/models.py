from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Product:
    source_id: str
    sku: str
    name: str
    description: str
    brand: str
    categories: list[str]
    attributes: dict[str, list[str]] = field(default_factory=dict)
    price: float | None = None
    currency: str = "EUR"
    in_stock: bool = True
    popularity: float = 0.0

    def searchable_text(self) -> str:
        flat_attributes = " ".join(
            f"{key} {' '.join(values)}"
            for key, values in sorted(self.attributes.items())
            if values
        )
        return " ".join(
            part
            for part in [
                self.sku,
                self.source_id,
                self.name,
                self.description,
                self.brand,
                " ".join(self.categories),
                flat_attributes,
            ]
            if part
        )


@dataclass(slots=True)
class StructuredQuery:
    raw_query: str
    normalized_text: str
    keywords: list[str]
    filters: dict[str, list[str]]
    boost_terms: list[str]
    intent: str
    explanation: str
    context_used: bool = False
    context_summary: str = ""


@dataclass(slots=True)
class SearchHit:
    product: Product
    score: float
    lexical_score: float
    semantic_score: float
    rerank_score: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    matched_filters: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class SearchResult:
    query: str
    structured_query: StructuredQuery
    hits: list[SearchHit]
    total_candidates: int
    retrieval_backend: str
    session_id: str
    assistant_message: str
    suggestion_chips: list[str] = field(default_factory=list)
