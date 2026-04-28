from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import (
    CatalogProductResponse,
    ProductDetailResponse,
    ReindexRequest,
    ReindexResponse,
)

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


@router.get("", response_model=list[CatalogProductResponse])
def list_catalog(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> list[dict]:
    products = request.app.state.search_application.list_products()
    if limit is not None:
        products = products[:limit]
    return [
        {
            "source_id": product.source_id,
            "sku": product.sku,
            "name": product.name,
            "brand": product.brand,
            "categories": product.categories,
            "attributes": product.attributes,
            "price": product.price,
            "currency": product.currency,
            "in_stock": product.in_stock,
            "popularity": product.popularity,
        }
        for product in products
    ]


@router.get("/{sku}", response_model=ProductDetailResponse)
def get_catalog_product(request: Request, sku: str) -> dict:
    product = request.app.state.search_application.get_product(sku)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable.")

    return {
        "source_id": product.source_id,
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "brand": product.brand,
        "categories": product.categories,
        "attributes": product.attributes,
        "price": product.price,
        "currency": product.currency,
        "in_stock": product.in_stock,
        "popularity": product.popularity,
    }


@router.post("/reindex", response_model=ReindexResponse)
def reindex_catalog(request: Request, payload: ReindexRequest) -> dict[str, str | int]:
    indexed_products = request.app.state.search_application.reindex(
        source=payload.source,
        source_path=payload.source_path,
        max_items=payload.max_items,
        sync_mode=payload.sync_mode,
        reset_index=payload.reset_index,
    )
    return {
        "indexed_products": indexed_products,
        "source": request.app.state.search_application.catalog_mode,
        "source_path": str(request.app.state.search_application.catalog_source),
        "sync_mode": payload.sync_mode,
    }
