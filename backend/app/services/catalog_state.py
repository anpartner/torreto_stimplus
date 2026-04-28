from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.domain.models import Product


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class SyncState:
    source: str = "sample"
    last_full_sync_at: str | None = None
    last_delta_sync_at: str | None = None
    last_akeneo_updated_at: str | None = None
    catalog_count: int = 0
    catalog_facets: dict[str, object] | None = None


class CatalogSnapshotRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> list[Product]:
        if not self._path.exists():
            return []

        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []

        products: list[Product] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or item.get("sku") or "")
            sku = str(item.get("sku") or source_id)
            name = str(item.get("name") or sku)
            products.append(
                Product(
                    source_id=source_id or sku,
                    sku=sku,
                    name=name,
                    description=str(item.get("description") or ""),
                    brand=str(item.get("brand") or "Unknown"),
                    categories=[str(value) for value in item.get("categories") or []],
                    attributes={
                        str(key): [str(entry) for entry in values]
                        for key, values in (item.get("attributes") or {}).items()
                        if isinstance(values, list)
                    },
                    price=float(item["price"]) if item.get("price") is not None else None,
                    currency=str(item.get("currency") or "EUR"),
                    in_stock=bool(item.get("in_stock", True)),
                    popularity=float(item.get("popularity") or 0.0),
                )
            )
        return products

    def save(self, products: list[Product]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(product) for product in products]
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


class SyncStateRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> SyncState:
        if not self._path.exists():
            return SyncState()

        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return SyncState()

        return SyncState(
            source=str(payload.get("source") or "sample"),
            last_full_sync_at=payload.get("last_full_sync_at"),
            last_delta_sync_at=payload.get("last_delta_sync_at"),
            last_akeneo_updated_at=payload.get("last_akeneo_updated_at"),
            catalog_count=int(payload.get("catalog_count") or 0),
            catalog_facets=payload.get("catalog_facets"),
        )

    def save(self, state: SyncState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()
