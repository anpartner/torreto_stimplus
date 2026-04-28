from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.settings import Settings
from app.domain.models import Product
from app.domain.text import extract_first_number, extract_sizes, normalize_text
from app.services.akeneo_client import AkeneoApiClient, AttributeMetadata
from app.services.catalog_state import (
    CatalogSnapshotRepository,
    SyncState,
    SyncStateRepository,
    utc_now_iso,
)


class InMemoryCatalogStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._products: list[Product] = []
        self._products_by_sku: dict[str, Product] = {}
        self._products_by_source_id: dict[str, Product] = {}

    def _refresh_indexes(self, products: list[Product]) -> None:
        self._products = list(products)
        self._products_by_sku = {product.sku: product for product in self._products if product.sku}
        self._products_by_source_id = {
            product.source_id: product for product in self._products if product.source_id
        }

    def replace(self, products: list[Product]) -> None:
        with self._lock:
            self._refresh_indexes(products)

    def upsert(self, products: list[Product]) -> list[Product]:
        with self._lock:
            indexed = {product.source_id: product for product in self._products}
            for product in products:
                indexed[product.source_id] = product
            merged_products = list(indexed.values())
            self._refresh_indexes(merged_products)
            return list(self._products)

    def clear(self) -> None:
        with self._lock:
            self._refresh_indexes([])

    def list(self) -> list[Product]:
        with self._lock:
            return list(self._products)

    def count(self) -> int:
        with self._lock:
            return len(self._products)

    def get_by_sku(self, sku: str) -> Product | None:
        with self._lock:
            return self._products_by_sku.get(sku)

    def get_by_source_id(self, source_id: str) -> Product | None:
        with self._lock:
            return self._products_by_source_id.get(source_id)


@dataclass(slots=True)
class CatalogSyncResult:
    products: list[Product]
    sync_mode: str
    changed_products: int
    checkpoint: str | None


class AkeneoCatalogNormalizer:
    CANONICAL_ATTRIBUTE_KEYS = ("product_family", "screen_size", "storage", "color")
    COMPUTER_ACCESSORY_MARKERS = (
        "accessoire",
        "accessoires",
        "station d accueil",
        "stations d accueil",
        "support",
        "supports",
        "filtre",
        "filtres",
        "coque",
        "coques",
        "clavier",
        "claviers",
        "cable",
        "cables",
        "adaptateur",
        "adaptateurs",
        "sac",
        "sacs",
        "sac a dos",
        "ventilateur",
        "ventilateurs",
        "refroidisseur",
        "refroidisseurs",
        "refroidissement",
        "onduleur",
        "ups",
        "boitier de piles",
        "boitiers d alimentation",
        "batterie",
        "batteries",
        "console kvm",
        "kvm",
        "audio et videoconferences",
        "audio et visioconferences",
        "audio et video",
        "barre video",
        "visioconference",
        "conference",
        "imprimante",
        "imprimantes",
        "cartouche",
        "cartouches",
        "service",
        "services",
        "logiciel",
        "logiciels",
    )
    LAPTOP_CATEGORY_MARKERS = (
        "ordinateurs portables",
        "portable pc",
    )
    DESKTOP_CATEGORY_MARKERS = (
        "ordinateurs bureau",
        "ordinateurs de bureau",
        "postes de travail",
        "stations de travail",
        "clients legers",
        "thin clients",
        "mini pc",
    )
    LAPTOP_NAME_MARKERS = (
        "ordinateur portable",
        "laptop",
        "notebook",
        "macbook",
        "vivobook",
        "thinkpad",
        "zenbook",
        "chromebook",
    )
    DESKTOP_NAME_MARKERS = (
        "ordinateur de bureau",
        "pc de bureau",
        "desktop",
        "station de travail",
        "workstation",
        "mini pc",
        "micro pc",
        "thin client",
        "client leger",
        "barebone",
    )
    ALL_IN_ONE_MARKERS = (
        "ordinateur tout en un",
        "ordinateur tout-en-un",
        "pc tout en un",
        "pc tout-en-un",
        "all in one pc",
        "aio pc",
    )
    NAME_KEYWORDS = (
        "name",
        "nom",
        "title",
        "titre",
        "designation",
        "libelle",
        "label",
    )
    DESCRIPTION_KEYWORDS = ("description", "summary", "resume", "presentation", "details")
    BRAND_KEYWORDS = ("brand", "marque", "manufacturer", "fabricant")
    STOCK_KEYWORDS = ("stock", "availability", "disponibilite", "dispo")
    PRICE_KEYWORDS = ("price", "prix", "tarif")
    POPULARITY_KEYWORDS = ("popular", "popularity", "score", "ranking")
    TEXT_TYPES = {"pim_catalog_text", "pim_catalog_textarea"}
    BRAND_TYPES = {"pim_catalog_text", "pim_catalog_textarea", "pim_catalog_simpleselect"}
    PRICE_TYPES = {"pim_catalog_price_collection"}
    SCREEN_SIZE_KEYWORDS = (
        "taille de la diagonale",
        "classe de diagonale",
        "screen size",
        "diagonale",
        "display size",
        "taille ecran",
        "taille de l ecran",
    )
    STORAGE_KEYWORDS = (
        "stockage",
        "storage",
        "capacite du disque dur",
        "capacite",
        "ssd",
        "hdd",
        "memoire flash",
    )
    COLOR_KEYWORDS = ("couleur", "color", "categorie de couleur")

    def __init__(
        self,
        preferred_locale: str = "fr_FR",
        fallback_locale: str = "en_US",
    ) -> None:
        self._preferred_locale = preferred_locale
        self._fallback_locale = fallback_locale

    def normalize(
        self,
        raw_item: dict[str, Any],
        attribute_metadata: dict[str, AttributeMetadata] | None = None,
        category_labels: dict[str, str] | None = None,
    ) -> Product:
        metadata = attribute_metadata or {}
        values = raw_item.get("values", {})

        identifier_code, identifier = self._extract_identifier(values)
        name_code, name = self._select_text_field(values, metadata, self.NAME_KEYWORDS, self.TEXT_TYPES)
        description_code, description = self._select_text_field(
            values,
            metadata,
            self.DESCRIPTION_KEYWORDS,
            self.TEXT_TYPES,
        )
        brand_code, brand = self._select_text_field(values, metadata, self.BRAND_KEYWORDS, self.BRAND_TYPES)
        price_code, price, currency = self._select_price(values, metadata)
        stock_code, in_stock = self._select_bool_field(values, metadata, self.STOCK_KEYWORDS)
        popularity_code, popularity = self._select_float_field(
            values,
            metadata,
            self.POPULARITY_KEYWORDS,
        )

        selected_codes = {
            code
            for code in [
                identifier_code,
                name_code,
                description_code,
                brand_code,
                price_code,
                stock_code,
                popularity_code,
            ]
            if code
        }

        attributes = self._build_attribute_index(values, metadata, selected_codes)
        brand = self._prefer_exact_attribute(attributes, brand, ("marque", "brand"))
        self._inject_canonical_attributes(
            name=name or "",
            description=description or "",
            categories=self._normalize_categories(raw_item.get("categories", []), category_labels),
            attributes=attributes,
        )
        categories = self._normalize_categories(raw_item.get("categories", []), category_labels)

        return Product(
            source_id=raw_item.get("uuid") or identifier or raw_item.get("identifier") or "",
            sku=identifier or raw_item.get("identifier") or raw_item.get("uuid", ""),
            name=name or identifier or raw_item.get("uuid", ""),
            description=description or "",
            brand=brand or "Unknown",
            categories=categories,
            attributes=attributes,
            price=price,
            currency=currency,
            in_stock=raw_item.get("enabled", True) if in_stock is None else in_stock,
            popularity=popularity if popularity is not None else 0.0,
        )

    def _build_attribute_index(
        self,
        values: dict[str, Any],
        metadata: dict[str, AttributeMetadata],
        selected_codes: set[str],
    ) -> dict[str, list[str]]:
        attributes: dict[str, list[str]] = {}

        for code, payload in values.items():
            if code in selected_codes:
                continue

            normalized_payload = self._normalize_attribute_payload(payload, metadata.get(code))
            if not normalized_payload:
                continue

            display_key = self._attribute_display_key(code, metadata)
            attributes[display_key] = normalized_payload

        return attributes

    def _prefer_exact_attribute(
        self,
        attributes: dict[str, list[str]],
        fallback: str | None,
        normalized_candidates: tuple[str, ...],
    ) -> str | None:
        for key, values in list(attributes.items()):
            if normalize_text(key) not in normalized_candidates:
                continue
            if not values:
                continue
            attributes.pop(key, None)
            return values[0]
        return fallback

    def _normalize_categories(
        self,
        categories: list[str],
        category_labels: dict[str, str] | None,
    ) -> list[str]:
        cleaned = []
        for category in categories:
            label = (category_labels or {}).get(category)
            text = label or category.replace("_", " ").strip().title()
            if text:
                cleaned.append(text)
        return cleaned

    def _extract_identifier(self, values: dict[str, Any]) -> tuple[str | None, str | None]:
        if "sku" in values:
            sku = self._first_text(values["sku"])
            if sku:
                return "sku", sku

        for code, payload in values.items():
            if not isinstance(payload, list):
                continue
            for item in payload:
                if item.get("attribute_type") == "pim_catalog_identifier":
                    data = item.get("data")
                    if isinstance(data, str) and data.strip():
                        return code, data.strip()

        for code, payload in values.items():
            candidate = self._first_text(payload)
            if candidate:
                return code, candidate
        return None, None

    def _select_text_field(
        self,
        values: dict[str, Any],
        metadata: dict[str, AttributeMetadata],
        keywords: tuple[str, ...],
        preferred_types: set[str],
    ) -> tuple[str | None, str | None]:
        best_score = 0
        best_match: tuple[str | None, str | None] = (None, None)

        for code, payload in values.items():
            text = self._first_text(payload)
            if not text:
                continue

            score = self._field_score(code, metadata.get(code), payload, keywords, preferred_types)
            if score > best_score:
                best_score = score
                best_match = (code, text)

        return best_match

    def _select_bool_field(
        self,
        values: dict[str, Any],
        metadata: dict[str, AttributeMetadata],
        keywords: tuple[str, ...],
    ) -> tuple[str | None, bool | None]:
        best_score = 0
        best_match: tuple[str | None, bool | None] = (None, None)

        for code, payload in values.items():
            boolean_value = self._first_bool(payload, default=None)
            if boolean_value is None:
                continue

            score = self._field_score(code, metadata.get(code), payload, keywords, set())
            if score > best_score:
                best_score = score
                best_match = (code, boolean_value)

        return best_match

    def _select_float_field(
        self,
        values: dict[str, Any],
        metadata: dict[str, AttributeMetadata],
        keywords: tuple[str, ...],
    ) -> tuple[str | None, float | None]:
        best_score = 0
        best_match: tuple[str | None, float | None] = (None, None)

        for code, payload in values.items():
            float_value = self._first_float(payload, default=None)
            if float_value is None:
                continue

            score = self._field_score(code, metadata.get(code), payload, keywords, set())
            if score > best_score:
                best_score = score
                best_match = (code, float_value)

        return best_match

    def _select_price(
        self,
        values: dict[str, Any],
        metadata: dict[str, AttributeMetadata],
    ) -> tuple[str | None, float | None, str]:
        best_score = 0
        best_match: tuple[str | None, float | None, str] = (None, None, "EUR")

        for code, payload in values.items():
            amount, currency = self._extract_price_from_payload(payload)
            if amount is None:
                continue

            score = self._field_score(code, metadata.get(code), payload, self.PRICE_KEYWORDS, self.PRICE_TYPES)
            if score > best_score:
                best_score = score
                best_match = (code, amount, currency)

        return best_match

    def _field_score(
        self,
        code: str,
        metadata: AttributeMetadata | None,
        payload: Any,
        keywords: tuple[str, ...],
        preferred_types: set[str],
    ) -> int:
        label = metadata.best_label(self._preferred_locale, self._fallback_locale) if metadata else code
        haystack = normalize_text(f"{code} {label}")
        score = 0

        for keyword in keywords:
            if keyword in haystack:
                score += 8

        if metadata and metadata.type in preferred_types:
            score += 3

        if any(isinstance(item.get("data"), str) and item.get("data", "").strip() for item in payload or []):
            score += 1

        if any(item.get("locale") == self._preferred_locale for item in payload or []):
            score += 1

        return score

    def _attribute_display_key(
        self,
        code: str,
        metadata: dict[str, AttributeMetadata],
    ) -> str:
        if code not in metadata:
            return code

        label = metadata[code].best_label(self._preferred_locale, self._fallback_locale)
        return label if label != code else code

    def _first_text(self, payload: Any) -> str | None:
        if not isinstance(payload, list):
            return None

        for preferred_locale in (self._preferred_locale, self._fallback_locale, None):
            for item in payload:
                if item.get("locale") != preferred_locale:
                    continue
                labels = self._linked_labels(item.get("linked_data"))
                if labels:
                    return labels[0]
                data = item.get("data")
                if isinstance(data, str) and data.strip():
                    return data.strip()

        return None

    def _first_bool(self, payload: Any, default: bool | None) -> bool | None:
        if not isinstance(payload, list):
            return default
        for item in payload:
            data = item.get("data")
            if isinstance(data, bool):
                return data
        return default

    def _first_float(self, payload: Any, default: float | None) -> float | None:
        if not isinstance(payload, list):
            return default
        for item in payload:
            data = item.get("data")
            if isinstance(data, (int, float)):
                value = float(data)
                if math.isfinite(value):
                    return value
                continue
            if isinstance(data, str):
                try:
                    value = float(data)
                    if math.isfinite(value):
                        return value
                except ValueError:
                    continue
        return default

    def _extract_price_from_payload(self, payload: Any) -> tuple[float | None, str]:
        if not isinstance(payload, list):
            return None, "EUR"

        for item in payload:
            data = item.get("data")
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and {"amount", "currency"} <= entry.keys():
                        try:
                            amount = float(entry["amount"])
                            if math.isfinite(amount):
                                return amount, str(entry["currency"])
                        except (TypeError, ValueError):
                            continue

            if isinstance(data, dict) and {"amount", "currency"} <= data.keys():
                try:
                    amount = float(data["amount"])
                    if math.isfinite(amount):
                        return amount, str(data["currency"])
                except (TypeError, ValueError):
                    continue

        return None, "EUR"

    def _normalize_attribute_payload(
        self,
        payload: Any,
        metadata: AttributeMetadata | None,
    ) -> list[str]:
        if not isinstance(payload, list):
            return []

        values: list[str] = []
        for item in payload:
            values.extend(self._humanize_value(item, metadata))

        deduped: list[str] = []
        for value in values:
            if value and value not in deduped:
                deduped.append(value)
        return deduped

    def _humanize_value(
        self,
        item: dict[str, Any],
        metadata: AttributeMetadata | None,
    ) -> list[str]:
        linked_labels = self._linked_labels(item.get("linked_data"))
        if linked_labels:
            return linked_labels

        data = item.get("data")
        if data is None:
            return []

        if isinstance(data, bool):
            return ["true" if data else "false"]

        if isinstance(data, list):
            values: list[str] = []
            for entry in data:
                values.extend(self._render_scalar(entry, metadata))
            return values

        return self._render_scalar(data, metadata)

    def _render_scalar(
        self,
        value: Any,
        metadata: AttributeMetadata | None,
    ) -> list[str]:
        if value is None:
            return []

        if isinstance(value, dict):
            if {"amount", "currency"} <= value.keys():
                return [f"{value['amount']} {value['currency']}"]
            if {"amount", "unit"} <= value.keys():
                return [f"{value['amount']} {value['unit']}"]
            return [f"{key}:{entry}" for key, entry in value.items() if entry is not None]

        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []

        if isinstance(value, (int, float)):
            return [str(value)]

        return [str(value)]

    def _linked_labels(self, linked_data: Any) -> list[str]:
        labels = self._collect_labels(linked_data)
        deduped: list[str] = []
        for label in labels:
            if label and label not in deduped:
                deduped.append(label)
        return deduped

    def enrich_product(self, product: Product) -> Product:
        self._inject_canonical_attributes(
            name=product.name,
            description=product.description,
            categories=product.categories,
            attributes=product.attributes,
        )
        return product

    def _inject_canonical_attributes(
        self,
        *,
        name: str,
        description: str,
        categories: list[str],
        attributes: dict[str, list[str]],
    ) -> None:
        source_attributes = {
            key: list(values)
            for key, values in attributes.items()
        }
        for key in self.CANONICAL_ATTRIBUTE_KEYS:
            attributes.pop(key, None)

        product_families = self._canonical_product_families(name, description, categories)
        if product_families:
            attributes["product_family"] = product_families

        screen_sizes = self._canonical_screen_sizes(name, categories, source_attributes, product_families)
        if screen_sizes:
            attributes["screen_size"] = screen_sizes

        storage_values = self._canonical_storage_values(source_attributes)
        if storage_values:
            attributes["storage"] = storage_values

        color_values = self._canonical_color_values(source_attributes)
        if color_values:
            attributes["color"] = color_values

    def _canonical_screen_sizes(
        self,
        name: str,
        categories: list[str],
        attributes: dict[str, list[str]],
        product_families: list[str],
    ) -> list[str]:
        sizes: list[str] = []
        category_text = normalize_text(" ".join(categories))
        can_use_name_sizes = bool(
            set(product_families) & {"computer", "laptop", "desktop", "all_in_one", "monitor", "tablet", "smartphone"}
        ) and "accessoires" not in category_text

        if can_use_name_sizes:
            for size in extract_sizes(name):
                self._append_unique(sizes, self._normalize_size_value(size))

        for key, values in attributes.items():
            normalized_key = normalize_text(key)
            if any(keyword in normalized_key for keyword in self.SCREEN_SIZE_KEYWORDS):
                for value in values:
                    for size in extract_sizes(value):
                        self._append_unique(sizes, self._normalize_size_value(size))
                    numeric_value = extract_first_number(value)
                    if numeric_value is not None and 3 <= numeric_value <= 120:
                        if "metrique" in normalized_key or "systeme metrique" in normalized_key:
                            inches_value = numeric_value / 2.54
                            if 3 <= inches_value <= 120:
                                self._append_unique(
                                    sizes,
                                    self._normalize_size_value(f"{inches_value} pouces"),
                                )
                        else:
                            self._append_unique(sizes, self._normalize_size_value(f"{numeric_value} pouces"))

        return [size for size in sizes if size]

    def _canonical_storage_values(self, attributes: dict[str, list[str]]) -> list[str]:
        storage_values: list[str] = []
        for key, values in attributes.items():
            normalized_key = normalize_text(key)
            if not any(keyword in normalized_key for keyword in self.STORAGE_KEYWORDS):
                continue
            for value in values:
                normalized_value = normalize_text(value)
                if "to" in normalized_value:
                    number = extract_first_number(value)
                    if number is not None:
                        self._append_unique(storage_values, f"{self._format_number(number)} to")
                if "go" in normalized_value:
                    number = extract_first_number(value)
                    if number is not None:
                        self._append_unique(storage_values, f"{self._format_number(number)} go")
        return storage_values

    def _canonical_color_values(self, attributes: dict[str, list[str]]) -> list[str]:
        colors: list[str] = []
        for key, values in attributes.items():
            normalized_key = normalize_text(key)
            if not any(keyword in normalized_key for keyword in self.COLOR_KEYWORDS):
                continue
            for value in values:
                cleaned = value.strip()
                if cleaned:
                    self._append_unique(colors, cleaned)
        return colors

    def _canonical_product_families(
        self,
        name: str,
        description: str,
        categories: list[str],
    ) -> list[str]:
        category_text = normalize_text(" ".join(categories))
        name_text = normalize_text(name)
        description_text = normalize_text(description)
        accessory_haystack = normalize_text(" ".join([name, " ".join(categories)]))
        haystack = normalize_text(" ".join([name, description, " ".join(categories)]))
        families: list[str] = []

        if "tablette" in category_text or "tablette" in name_text or "tablet" in name_text:
            self._append_unique(families, "tablet")
            return families

        if any(token in category_text for token in ("telephone", "smartphone")) or any(
            token in name_text for token in ("smartphone", "telephone", "mobile")
        ):
            self._append_unique(families, "smartphone")
            return families

        if any(token in category_text for token in ("ecrans", "moniteur", "signalisation")) or any(
            token in name_text for token in ("ecran", "moniteur", "monitor", "display")
        ):
            self._append_unique(families, "monitor")
            return families

        if any(token in accessory_haystack for token in self.COMPUTER_ACCESSORY_MARKERS):
            return families

        if any(token in haystack for token in self.ALL_IN_ONE_MARKERS):
            self._append_unique(families, "computer")
            self._append_unique(families, "all_in_one")
            return families

        if any(token in category_text for token in self.LAPTOP_CATEGORY_MARKERS) or any(
            token in haystack for token in self.LAPTOP_NAME_MARKERS
        ):
            self._append_unique(families, "computer")
            self._append_unique(families, "laptop")
            return families

        if any(token in category_text for token in self.DESKTOP_CATEGORY_MARKERS) or any(
            token in haystack for token in self.DESKTOP_NAME_MARKERS
        ):
            self._append_unique(families, "computer")
            self._append_unique(families, "desktop")
            return families

        if any(token in description_text for token in self.LAPTOP_NAME_MARKERS):
            self._append_unique(families, "computer")
            self._append_unique(families, "laptop")
            return families

        if (
            any(token in category_text for token in self.LAPTOP_CATEGORY_MARKERS + self.DESKTOP_CATEGORY_MARKERS)
            or "ordinateur" in name_text
            or "computer" in name_text
            or "ai pc" in name_text
            or "copilot pc" in name_text
        ):
            self._append_unique(families, "computer")

        return families

    def _normalize_size_value(self, value: str) -> str:
        numeric_value = extract_first_number(value)
        if numeric_value is None:
            return value
        return f"{self._format_number(numeric_value)} pouces"

    def _format_number(self, value: float) -> str:
        return str(int(value)) if float(value).is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")

    def _append_unique(self, values: list[str], candidate: str | None) -> None:
        if candidate and candidate not in values:
            values.append(candidate)

    def _collect_labels(self, node: Any) -> list[str]:
        if node is None:
            return []

        if isinstance(node, dict):
            labels = node.get("labels")
            if isinstance(labels, dict):
                label = (
                    labels.get(self._preferred_locale)
                    or labels.get(self._fallback_locale)
                    or next(iter(labels.values()), None)
                )
                return [label] if isinstance(label, str) and label.strip() else []

            if "label" in node and isinstance(node["label"], str) and node["label"].strip():
                return [node["label"].strip()]

            flattened: list[str] = []
            for value in node.values():
                flattened.extend(self._collect_labels(value))
            return flattened

        if isinstance(node, list):
            flattened: list[str] = []
            for item in node:
                flattened.extend(self._collect_labels(item))
            return flattened

        return []


class CatalogIngestionService:
    def __init__(self, store: InMemoryCatalogStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        self._normalizer = AkeneoCatalogNormalizer(
            preferred_locale=settings.akeneo.preferred_locale,
            fallback_locale=settings.akeneo.fallback_locale,
        )
        self._akeneo_client = AkeneoApiClient(settings.akeneo)
        self._snapshot_repository = CatalogSnapshotRepository(settings.catalog_snapshot_path)
        self._state_repository = SyncStateRepository(settings.sync_state_path)

    def reindex(self, source_path: str | Path) -> list[Product]:
        return self.reindex_from_file(source_path)

    def reindex_from_file(self, source_path: str | Path) -> list[Product]:
        raw_items = self._load_json(Path(source_path))
        products = [self._normalizer.normalize(item) for item in raw_items]
        self._store.replace(products)
        self._snapshot_repository.save(products)
        self._state_repository.save(
            SyncState(
                source="sample",
                last_full_sync_at=utc_now_iso(),
                catalog_count=len(products),
                catalog_facets=self._build_catalog_facets(products),
            )
        )
        return products

    def reindex_from_akeneo(self, max_products: int | None = None) -> list[Product]:
        return self.sync_from_akeneo(sync_mode="full", max_products=max_products).products

    def sync_from_akeneo(
        self,
        *,
        sync_mode: str,
        max_products: int | None = None,
    ) -> CatalogSyncResult:
        chosen_max = max_products if max_products is not None else self._settings.akeneo.max_products
        if sync_mode == "delta":
            return self._delta_sync_from_akeneo(chosen_max)

        logger = logging.getLogger(__name__)
        attribute_metadata = self._akeneo_client.list_attributes()
        category_labels = self._akeneo_client.list_categories()
        products: list[Product] = []
        checkpoint: str | None = None

        for raw_batch in self._akeneo_client.iter_product_batches(max_products=chosen_max):
            normalized_batch = [
                self._normalizer.normalize(item, attribute_metadata, category_labels)
                for item in raw_batch
            ]
            products.extend(normalized_batch)
            batch_checkpoint = self._max_updated_at(raw_batch)
            if batch_checkpoint and (checkpoint is None or batch_checkpoint > checkpoint):
                checkpoint = batch_checkpoint
            logger.info("Akeneo full sync progress: %s products normalized", len(products))

        self._store.replace(products)
        self._snapshot_repository.save(products)
        catalog_facets = self._build_catalog_facets(products)
        if chosen_max is None:
            self._state_repository.save(
                SyncState(
                    source="akeneo",
                    last_full_sync_at=utc_now_iso(),
                    last_akeneo_updated_at=checkpoint,
                    catalog_count=len(products),
                    catalog_facets=catalog_facets,
                )
            )
        return CatalogSyncResult(
            products=products,
            sync_mode="full",
            changed_products=len(products),
            checkpoint=checkpoint,
        )

    def load_persisted_catalog(self) -> list[Product]:
        products = [self._normalizer.enrich_product(product) for product in self._snapshot_repository.load()]
        if products:
            self._store.replace(products)
        return products

    def has_persisted_catalog(self) -> bool:
        return self._snapshot_repository.exists()

    def reset_local_catalog(self) -> None:
        self._store.clear()
        self._snapshot_repository.clear()
        self._state_repository.clear()

    def list_products(self) -> list[Product]:
        return self._store.list()

    def count(self) -> int:
        return self._store.count()

    def get_sync_state(self) -> SyncState:
        return self._state_repository.load()

    def _delta_sync_from_akeneo(self, max_products: int | None) -> CatalogSyncResult:
        state = self._state_repository.load()
        checkpoint = state.last_akeneo_updated_at
        if not checkpoint:
            return self.sync_from_akeneo(sync_mode="full", max_products=max_products)

        had_full_catalog_in_memory = (
            state.catalog_count > 0 and self._store.count() >= state.catalog_count
        )
        raw_items, attribute_metadata, category_labels = self._akeneo_client.export_catalog_delta(
            updated_after=checkpoint,
            max_products=max_products,
        )
        products = [
            self._normalizer.normalize(item, attribute_metadata, category_labels)
            for item in raw_items
        ]
        merged_products = self._store.upsert(products)
        if had_full_catalog_in_memory:
            self._snapshot_repository.save(merged_products)
        new_checkpoint = self._max_updated_at(raw_items) or checkpoint

        if max_products is None:
            catalog_count = max(state.catalog_count, len(merged_products))
            self._state_repository.save(
                SyncState(
                    source="akeneo",
                    last_full_sync_at=state.last_full_sync_at,
                    last_delta_sync_at=utc_now_iso(),
                    last_akeneo_updated_at=new_checkpoint,
                    catalog_count=catalog_count,
                    catalog_facets=state.catalog_facets,
                )
            )

        return CatalogSyncResult(
            products=products,
            sync_mode="delta",
            changed_products=len(products),
            checkpoint=new_checkpoint,
        )

    def _load_json(self, source_path: Path) -> list[dict[str, Any]]:
        payload = json.loads(source_path.read_text(encoding="utf-8"))

        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload["items"]

        raise ValueError(f"Unsupported catalog payload in {source_path}")

    def _max_updated_at(self, raw_items: list[dict[str, Any]]) -> str | None:
        timestamps = [
            str(item.get("updated"))
            for item in raw_items
            if item.get("updated")
        ]
        return max(timestamps) if timestamps else None

    def _build_catalog_facets(self, products: list[Product]) -> dict[str, object]:
        brand_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        attribute_counts: dict[str, dict[str, int]] = {
            "color": {},
            "storage": {},
            "screen_size": {},
            "product_family": {},
        }

        for product in products:
            if product.brand:
                brand_counts[product.brand] = brand_counts.get(product.brand, 0) + 1

            for category in product.categories:
                category_counts[category] = category_counts.get(category, 0) + 1

            for key in attribute_counts:
                for value in product.attributes.get(key, []):
                    attribute_counts[key][value] = attribute_counts[key].get(value, 0) + 1

        def top_values(counts: dict[str, int], limit: int) -> list[str]:
            ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            return [value for value, _ in ordered[:limit]]

        return {
            "brands": top_values(brand_counts, 60),
            "categories": top_values(category_counts, 60),
            "attributes": {
                key: top_values(values, 30)
                for key, values in attribute_counts.items()
                if values
            },
        }
