from __future__ import annotations

import re
import unicodedata

STOPWORDS = {
    "a",
    "au",
    "aux",
    "avec",
    "ce",
    "ces",
    "de",
    "des",
    "du",
    "en",
    "et",
    "je",
    "la",
    "le",
    "les",
    "mes",
    "mon",
    "pour",
    "sur",
    "un",
    "une",
}

TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
SIZE_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(pouces|pouce|inch|inches)")
QUOTED_SIZE_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:\"|”|′′)")
STORAGE_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(go|gb|gib|to|tb|tib)\b")
SSD_STORAGE_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*ssd\b")
COMPACT_UNIT_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(gb|gib|go|tb|tib|to)\b")


def _normalize_compact_units(text: str) -> str:
    normalized = text or ""

    def replace(match: re.Match[str]) -> str:
        number = match.group(1)
        unit = match.group(2).lower()
        normalized_unit = "to" if unit in {"tb", "tib", "to"} else "go"
        return f"{number} {normalized_unit}"

    normalized = COMPACT_UNIT_PATTERN.sub(replace, normalized)
    normalized = re.sub(r"\bsdd\b", "ssd", normalized, flags=re.IGNORECASE)
    return normalized


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", _normalize_compact_units(text))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(cleaned.split())


def normalize_measurement_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", _normalize_compact_units(text))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    cleaned = re.sub(r"[^a-z0-9.,\"'\s]+", " ", lowered)
    return " ".join(cleaned.split())


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in TOKEN_PATTERN.findall(normalize_text(text))
        if token not in STOPWORDS
    ]


def extract_sizes(text: str) -> list[str]:
    sizes: list[str] = []
    for number, unit in SIZE_PATTERN.findall(normalize_measurement_text(text)):
        unit_label = "pouces" if unit.startswith("pou") or unit.startswith("inc") else unit
        sizes.append(f"{number.replace(',', '.')} {unit_label}")
    for number in QUOTED_SIZE_PATTERN.findall(text or ""):
        sizes.append(f"{number.replace(',', '.')} pouces")
    return sizes


def extract_storage_values(text: str) -> list[str]:
    values: list[str] = []
    normalized_text = normalize_measurement_text(text)
    for number, unit in STORAGE_PATTERN.findall(normalized_text):
        normalized_unit = "to" if unit in {"to", "tb", "tib"} else "go"
        candidate = f"{number.replace(',', '.')} {normalized_unit}"
        if candidate not in values:
            values.append(candidate)
    for number in SSD_STORAGE_PATTERN.findall(normalized_text):
        candidate = f"{number.replace(',', '.')} go"
        if candidate not in values:
            values.append(candidate)
    return values


def extract_first_number(text: str) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def char_trigrams(text: str) -> set[str]:
    normalized = f"  {normalize_text(text)}  "
    if len(normalized.strip()) < 3:
        return {normalized.strip()} if normalized.strip() else set()
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)
