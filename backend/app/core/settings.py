from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AkeneoSettings:
    base_url: str | None
    username: str | None
    password: str | None
    client_id: str | None
    client_secret: str | None
    preferred_locale: str
    fallback_locale: str
    page_limit: int
    max_products: int | None

    def is_configured(self) -> bool:
        return all(
            [
                self.base_url,
                self.username,
                self.password,
                self.client_id,
                self.client_secret,
            ]
        )


@dataclass(slots=True)
class TypesenseSettings:
    url: str | None
    api_key: str | None
    collection_name: str
    import_batch_size: int
    candidate_multiplier: int

    def is_configured(self) -> bool:
        return bool(self.url and self.api_key and self.collection_name)


@dataclass(slots=True)
class GroqSettings:
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: int

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


@dataclass(slots=True)
class CohereSettings:
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: int

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


@dataclass(slots=True)
class Settings:
    app_name: str
    api_prefix: str
    frontend_origin: str
    sample_catalog_path: Path
    catalog_snapshot_path: Path
    sync_state_path: Path
    catalog_mode: str
    default_limit: int
    akeneo: AkeneoSettings
    typesense: TypesenseSettings
    groq: GroqSettings
    cohere: CohereSettings


def get_settings() -> Settings:
    backend_root = Path(__file__).resolve().parents[2]

    return Settings(
        app_name="AI Ecommerce Search",
        api_prefix="/api/v1",
        frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:3000"),
        sample_catalog_path=Path(
            os.getenv(
                "CATALOG_SOURCE_PATH",
                backend_root / "data" / "sample_catalog.json",
            )
        ).resolve(),
        catalog_snapshot_path=Path(
            os.getenv(
                "CATALOG_SNAPSHOT_PATH",
                backend_root / "data" / "runtime" / "catalog_snapshot.json",
            )
        ).resolve(),
        sync_state_path=Path(
            os.getenv(
                "SYNC_STATE_PATH",
                backend_root / "data" / "runtime" / "sync_state.json",
            )
        ).resolve(),
        catalog_mode=os.getenv("CATALOG_MODE", "sample").strip().lower() or "sample",
        default_limit=6,
        akeneo=AkeneoSettings(
            base_url=os.getenv("AKENEO_BASE_URL"),
            username=os.getenv("AKENEO_USERNAME"),
            password=os.getenv("AKENEO_PASSWORD"),
            client_id=os.getenv("AKENEO_CLIENT_ID"),
            client_secret=os.getenv("AKENEO_CLIENT_SECRET"),
            preferred_locale=os.getenv("AKENEO_PREFERRED_LOCALE", "fr_FR"),
            fallback_locale=os.getenv("AKENEO_FALLBACK_LOCALE", "en_US"),
            page_limit=int(os.getenv("AKENEO_PAGE_LIMIT", "100")),
            max_products=(
                int(os.getenv("AKENEO_MAX_PRODUCTS", "0")) or None
            ),
        ),
        typesense=TypesenseSettings(
            url=os.getenv("TYPESENSE_URL"),
            api_key=os.getenv("TYPESENSE_API_KEY"),
            collection_name=os.getenv("TYPESENSE_COLLECTION", "products"),
            import_batch_size=int(os.getenv("TYPESENSE_IMPORT_BATCH_SIZE", "250")),
            candidate_multiplier=int(os.getenv("TYPESENSE_CANDIDATE_MULTIPLIER", "8")),
        ),
        groq=GroqSettings(
            api_key=os.getenv("GROQ_API_KEY"),
            base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            timeout_seconds=int(os.getenv("GROQ_TIMEOUT_SECONDS", "20")),
        ),
        cohere=CohereSettings(
            api_key=os.getenv("COHERE_API_KEY"),
            base_url=os.getenv("COHERE_BASE_URL", "https://api.cohere.com/v2"),
            model=os.getenv("COHERE_RERANK_MODEL", "rerank-v4.0-fast"),
            timeout_seconds=int(os.getenv("COHERE_TIMEOUT_SECONDS", "20")),
        ),
    )
