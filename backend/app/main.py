from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.catalog import router as catalog_router
from app.api.routes.health import router as health_router
from app.api.routes.search import router as search_router
from app.core.settings import get_settings
from app.services.search_engine import build_search_application


def create_app() -> FastAPI:
    settings = get_settings()
    search_application = build_search_application(settings)
    allowed_origins = {
        settings.frontend_origin,
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    }

    app = FastAPI(title=settings.app_name, version="0.1.0")
    app.state.search_application = search_application

    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def startup() -> None:
        search_application.bootstrap()

    app.include_router(health_router)
    app.include_router(catalog_router)
    app.include_router(search_router)
    return app


app = create_app()
