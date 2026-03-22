from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Tuple


def _parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_widths(value: str) -> Tuple[int, ...]:
    if not value:
        return ()
    widths = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        widths.append(int(item))
    return tuple(sorted(set(widths)))


def _parse_thumbnail_format(value: str | None) -> str:
    if not value:
        return "jpeg"
    value = value.strip().lower()
    if value not in {"jpeg", "png", "webp"}:
        raise ValueError(f"Unsupported thumbnail format: {value}")
    return value


def _parse_api_keys(value: str | None) -> FrozenSet[str]:
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    storage_root: Path
    public_prefix: str
    max_upload_bytes: int
    enable_thumbnails: bool
    thumbnail_widths: Tuple[int, ...]
    thumbnail_format: str
    api_keys: FrozenSet[str]
    pg_database: str | None

    @property
    def original_root(self) -> Path:
        return self.storage_root / "original"

    @property
    def variants_root(self) -> Path:
        return self.storage_root / "variants"


def load_settings() -> Settings:
    public_prefix = os.getenv("IMAGE_PUBLIC_PREFIX", "/i").rstrip("/")
    if not public_prefix.startswith("/"):
        public_prefix = "/" + public_prefix

    return Settings(
        host=os.getenv("IMAGE_UPLOAD_HOST", "127.0.0.1"),
        port=int(os.getenv("IMAGE_UPLOAD_PORT", "8080")),
        storage_root=Path(os.getenv("IMAGE_STORAGE_ROOT", "./data")).expanduser().resolve(),
        public_prefix=public_prefix,
        max_upload_bytes=int(os.getenv("IMAGE_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        enable_thumbnails=_parse_bool(os.getenv("IMAGE_ENABLE_THUMBNAILS"), True),
        thumbnail_widths=_parse_widths(os.getenv("IMAGE_THUMBNAIL_WIDTHS", "160,320,640")),
        thumbnail_format=_parse_thumbnail_format(os.getenv("IMAGE_THUMBNAIL_FORMAT")),
        api_keys=_parse_api_keys(os.getenv("IMAGE_API_KEYS")),
        pg_database=os.getenv("DATABASE_URL") or os.getenv("PGDATABASE"),
    )
