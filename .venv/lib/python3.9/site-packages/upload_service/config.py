from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Tuple


def _parse_bool(value: str, default: bool) -> bool:
    # 환경변수의 다양한 진리값 표현을 하나로 정규화한다.
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_widths(value: str) -> Tuple[int, ...]:
    # "160,320,640" 같은 설정값을 정렬된 정수 튜플로 바꾼다.
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
    # 현재 구현은 sips가 지원하는 포맷만 허용한다.
    if not value:
        return "jpeg"
    value = value.strip().lower()
    if value not in {"jpeg", "png", "webp"}:
        raise ValueError(f"Unsupported thumbnail format: {value}")
    return value


def _parse_api_keys(value: str | None) -> FrozenSet[str]:
    # 여러 키를 허용할 수 있게 쉼표 구분 문자열을 집합으로 바꾼다.
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    """서비스 전체에서 공유하는 실행 설정."""

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
        # 원본 이미지는 original 트리 아래에 저장한다.
        return self.storage_root / "original"

    @property
    def variants_root(self) -> Path:
        # 썸네일/파생 이미지는 variants 트리 아래에 저장한다.
        return self.storage_root / "variants"


def load_settings() -> Settings:
    # 공개 URL prefix는 항상 "/..." 형태가 되도록 보정한다.
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
