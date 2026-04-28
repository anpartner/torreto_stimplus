from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, description="Natural language user query")
    limit: int = Field(default=6, ge=1, le=60)
    session_id: str | None = None
    visitor_id: str | None = None
    reset_context: bool = False
    previous_context: "StructuredQueryContextRequest | None" = None


class StructuredQueryContextRequest(BaseModel):
    raw_query: str
    normalized_text: str
    keywords: list[str]
    filters: dict[str, list[str]]
    boost_terms: list[str]
    intent: str
    explanation: str
    context_used: bool = False
    context_summary: str = ""


class ReindexRequest(BaseModel):
    source: Literal["sample", "akeneo"] | None = None
    source_path: str | None = None
    max_items: int | None = Field(default=None, ge=1, le=5000)
    sync_mode: Literal["full", "delta"] = "full"
    reset_index: bool = False


class StructuredQueryResponse(BaseModel):
    raw_query: str
    normalized_text: str
    keywords: list[str]
    filters: dict[str, list[str]]
    boost_terms: list[str]
    intent: str
    explanation: str
    context_used: bool
    context_summary: str


class SearchHitResponse(BaseModel):
    source_id: str
    sku: str
    name: str
    brand: str
    categories: list[str]
    price: float | None
    currency: str
    in_stock: bool
    matched_terms: list[str]
    matched_filters: dict[str, list[str]]


class SearchResponse(BaseModel):
    query: str
    structured_query: StructuredQueryResponse
    hits: list[SearchHitResponse]
    total_candidates: int
    retrieval_backend: str
    session_id: str
    assistant_message: str
    suggestion_chips: list[str]


class CatalogProductResponse(BaseModel):
    source_id: str
    sku: str
    name: str
    brand: str
    categories: list[str]
    attributes: dict[str, list[str]]
    price: float | None
    currency: str
    in_stock: bool
    popularity: float


class ProductDetailResponse(BaseModel):
    source_id: str
    sku: str
    name: str
    description: str
    brand: str
    categories: list[str]
    attributes: dict[str, list[str]]
    price: float | None
    currency: str
    in_stock: bool
    popularity: float


class ReindexResponse(BaseModel):
    indexed_products: int
    source: str
    source_path: str
    sync_mode: str
