from __future__ import annotations

import os
from pathlib import Path

from .config import load_database_settings
from .db import Database, _sql_literal


def _normalize_root(value: str, variable_name: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized or not Path(normalized).is_absolute():
        raise ValueError(f"{variable_name} must be an absolute path")
    return normalized


def update_storage_root(database: Database, old_root: str, new_root: str) -> None:
    """원본과 파생 이미지 경로를 하나의 PostgreSQL 트랜잭션에서 보정한다."""

    old_literal = _sql_literal(old_root)
    new_literal = _sql_literal(new_root)
    database.run_sql(
        f"""
BEGIN;
DO $$
BEGIN
    IF to_regclass('assets') IS NULL OR to_regclass('asset_variants') IS NULL THEN
        RAISE EXCEPTION 'image metadata tables do not exist in the selected database';
    END IF;
END
$$;

UPDATE assets
SET storage_path = {new_literal} || substr(storage_path, length({old_literal}) + 1)
WHERE left(storage_path, length({old_literal}) + 1) = {old_literal} || '/';

UPDATE asset_variants
SET storage_path = {new_literal} || substr(storage_path, length({old_literal}) + 1)
WHERE left(storage_path, length({old_literal}) + 1) = {old_literal} || '/';
COMMIT;
"""
    )


def main() -> None:
    settings = load_database_settings(require_explicit_target=True)
    old_root = _normalize_root(
        os.getenv(
            "OLD_STORAGE_ROOT",
            str(Path.home() / "mnt/gorani-images/image-store"),
        ),
        "OLD_STORAGE_ROOT",
    )
    new_root = _normalize_root(
        os.getenv("NEW_STORAGE_ROOT", "/Volumes/gorani-images/image-store"),
        "NEW_STORAGE_ROOT",
    )
    if old_root == new_root:
        raise ValueError("OLD_STORAGE_ROOT and NEW_STORAGE_ROOT must differ")

    update_storage_root(Database(settings), old_root, new_root)
    print("Updated storage root:")
    print(f"  from: {old_root}")
    print(f"    to: {new_root}")


if __name__ == "__main__":
    main()
