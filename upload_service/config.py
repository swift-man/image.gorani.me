from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Tuple
from urllib.parse import urlsplit


def _parse_bool(value: str | None, default: bool) -> bool:
    # 환경변수의 다양한 진리값 표현을 하나로 정규화한다.
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Unsupported boolean value: {value}")


def _parse_widths(value: str) -> Tuple[int, ...]:
    # "160,320,640" 같은 설정값을 정렬된 정수 튜플로 바꾼다.
    if not value:
        return ()
    widths = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        width = int(item)
        if width <= 0:
            raise ValueError("IMAGE_THUMBNAIL_WIDTHS must contain only positive integers")
        widths.append(width)
    return tuple(sorted(set(widths)))


def _parse_thumbnail_format(value: str | None) -> str:
    # 현재 썸네일 생성기가 지원하는 출력 포맷만 허용한다.
    if not value:
        return "webp"
    value = value.strip().lower()
    if value not in {"jpeg", "png", "webp"}:
        raise ValueError(f"Unsupported thumbnail format: {value}")
    return value


def _parse_api_keys(value: str | None) -> FrozenSet[str]:
    # 여러 키를 허용할 수 있게 쉼표 구분 문자열을 집합으로 바꾼다.
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _normalize_public_prefix(value: str) -> str:
    # 루트 경로는 조합 시 // URL이 되므로 최소 한 개의 경로 세그먼트를 요구한다.
    normalized = value.strip().strip("/")
    if not normalized:
        raise ValueError("IMAGE_PUBLIC_PREFIX must include a path such as /i")
    return "/" + normalized


def _parse_positive_int(name: str, value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _load_dotenv() -> None:
    # 프로젝트 루트의 .env를 읽어 셸에 아직 없는 변수만 채운다(셸 값이 우선).
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    """서비스 전체에서 공유하는 실행 설정."""

    host: str
    port: int
    storage_root: Path
    require_storage_mount: bool
    public_prefix: str
    max_upload_bytes: int
    max_image_pixels: int
    max_workers: int
    http_timeout_seconds: int
    cleanup_batch_size: int
    command_timeout_seconds: int
    db_timeout_seconds: int
    enable_thumbnails: bool
    thumbnail_widths: Tuple[int, ...]
    thumbnail_format: str
    api_keys: FrozenSet[str]
    database_url: str | None
    pg_database: str | None

    @property
    def original_root(self) -> Path:
        # 원본 이미지는 original 트리 아래에 저장한다.
        return self.storage_root / "original"

    @property
    def variants_root(self) -> Path:
        # 썸네일/파생 이미지는 variants 트리 아래에 저장한다.
        return self.storage_root / "variants"


@dataclass(frozen=True)
class PostgreSQLSettings:
    """서비스와 관리 명령이 공유하는 PostgreSQL 접속 설정."""

    database_url: str | None
    pg_database: str | None
    db_timeout_seconds: int


def _database_settings_from_environment(
    require_explicit_target: bool,
) -> PostgreSQLSettings:
    raw_database_url = os.getenv("DATABASE_URL")
    database_url = raw_database_url.strip() if raw_database_url else None
    raw_pg_database = os.getenv("PGDATABASE")
    pg_database = raw_pg_database.strip() if raw_pg_database else None
    if database_url and not database_url.startswith(("postgres://", "postgresql://")):
        raise ValueError("DATABASE_URL must use the postgres:// or postgresql:// scheme")
    database_url_has_name = bool(
        database_url and urlsplit(database_url).path.strip("/")
    )
    if require_explicit_target and not database_url_has_name and not pg_database:
        raise ValueError("DATABASE_URL or PGDATABASE must explicitly select a database")
    return PostgreSQLSettings(
        database_url=database_url,
        pg_database=pg_database,
        db_timeout_seconds=_parse_positive_int(
            "IMAGE_DB_TIMEOUT_SECONDS",
            os.getenv("IMAGE_DB_TIMEOUT_SECONDS", "10"),
        ),
    )


def load_database_settings(
    require_explicit_target: bool = False,
) -> PostgreSQLSettings:
    _load_dotenv()
    return _database_settings_from_environment(require_explicit_target)


def load_settings() -> Settings:
    _load_dotenv()
    database_settings = _database_settings_from_environment(False)
    public_prefix = _normalize_public_prefix(os.getenv("IMAGE_PUBLIC_PREFIX", "/i"))
    api_keys = _parse_api_keys(os.getenv("IMAGE_API_KEYS"))
    if not api_keys:
        raise ValueError("IMAGE_API_KEYS must contain at least one API key")
    max_upload_bytes = _parse_positive_int(
        "IMAGE_MAX_UPLOAD_BYTES",
        os.getenv("IMAGE_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)),
    )
    max_image_pixels = _parse_positive_int(
        "IMAGE_MAX_IMAGE_PIXELS",
        os.getenv("IMAGE_MAX_IMAGE_PIXELS", "40000000"),
    )
    max_workers = _parse_positive_int(
        "IMAGE_MAX_WORKERS",
        os.getenv("IMAGE_MAX_WORKERS", "8"),
    )
    http_timeout_seconds = _parse_positive_int(
        "IMAGE_HTTP_TIMEOUT_SECONDS",
        os.getenv("IMAGE_HTTP_TIMEOUT_SECONDS", "30"),
    )
    cleanup_batch_size = _parse_positive_int(
        "IMAGE_CLEANUP_BATCH_SIZE",
        os.getenv("IMAGE_CLEANUP_BATCH_SIZE", "10"),
    )
    command_timeout_seconds = _parse_positive_int(
        "IMAGE_COMMAND_TIMEOUT_SECONDS",
        os.getenv("IMAGE_COMMAND_TIMEOUT_SECONDS", "30"),
    )
    return Settings(
        host=os.getenv("IMAGE_UPLOAD_HOST", "127.0.0.1"),
        port=int(os.getenv("IMAGE_UPLOAD_PORT", "8080")),
        storage_root=Path(
            os.getenv("IMAGE_STORAGE_ROOT", "/Volumes/gorani-images/image-store")
        ).expanduser().resolve(),
        require_storage_mount=_parse_bool(os.getenv("IMAGE_REQUIRE_STORAGE_MOUNT"), True),
        public_prefix=public_prefix,
        max_upload_bytes=max_upload_bytes,
        max_image_pixels=max_image_pixels,
        max_workers=max_workers,
        http_timeout_seconds=http_timeout_seconds,
        cleanup_batch_size=cleanup_batch_size,
        command_timeout_seconds=command_timeout_seconds,
        db_timeout_seconds=database_settings.db_timeout_seconds,
        enable_thumbnails=_parse_bool(os.getenv("IMAGE_ENABLE_THUMBNAILS"), True),
        thumbnail_widths=_parse_widths(os.getenv("IMAGE_THUMBNAIL_WIDTHS", "160,320,640")),
        thumbnail_format=_parse_thumbnail_format(os.getenv("IMAGE_THUMBNAIL_FORMAT")),
        api_keys=api_keys,
        database_url=database_settings.database_url,
        pg_database=database_settings.pg_database or "postgres",
    )
