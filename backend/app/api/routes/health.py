from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
def healthcheck(request: Request) -> dict[str, str | int]:
    return request.app.state.search_application.health()

