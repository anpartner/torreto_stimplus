from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas import SearchRequest, SearchResponse
from app.domain.models import StructuredQuery

router = APIRouter(prefix="/api/v1/search", tags=["search"])


@router.post("", response_model=SearchResponse)
def search(request: Request, payload: SearchRequest) -> dict:
    previous_context = None
    if payload.previous_context is not None:
        previous_context = StructuredQuery(
            raw_query=payload.previous_context.raw_query,
            normalized_text=payload.previous_context.normalized_text,
            keywords=payload.previous_context.keywords,
            filters=payload.previous_context.filters,
            boost_terms=payload.previous_context.boost_terms,
            intent=payload.previous_context.intent,
            explanation=payload.previous_context.explanation,
            context_used=payload.previous_context.context_used,
            context_summary=payload.previous_context.context_summary,
        )

    result = request.app.state.search_application.search(
        query=payload.query,
        limit=payload.limit,
        session_id=payload.session_id,
        visitor_id=payload.visitor_id,
        reset_context=payload.reset_context,
        previous_structured_query=previous_context,
    )

    return {
        "query": result.query,
        "session_id": result.session_id,
        "assistant_message": result.assistant_message,
        "suggestion_chips": result.suggestion_chips,
        "structured_query": {
            "raw_query": result.structured_query.raw_query,
            "normalized_text": result.structured_query.normalized_text,
            "keywords": result.structured_query.keywords,
            "filters": result.structured_query.filters,
            "boost_terms": result.structured_query.boost_terms,
            "intent": result.structured_query.intent,
            "explanation": result.structured_query.explanation,
            "context_used": result.structured_query.context_used,
            "context_summary": result.structured_query.context_summary,
        },
        "hits": [
            {
                "source_id": hit.product.source_id,
                "sku": hit.product.sku,
                "name": hit.product.name,
                "brand": hit.product.brand,
                "categories": hit.product.categories,
                "price": hit.product.price,
                "currency": hit.product.currency,
                "in_stock": hit.product.in_stock,
                "matched_terms": hit.matched_terms,
                "matched_filters": hit.matched_filters,
            }
            for hit in result.hits
        ],
        "total_candidates": result.total_candidates,
        "retrieval_backend": result.retrieval_backend,
    }
