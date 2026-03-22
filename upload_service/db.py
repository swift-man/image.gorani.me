from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .config import Settings


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("'", "''")
    return "'" + text + "'"


@dataclass
class VariantRecord:
    kind: str
    format: str
    width: int
    height: int
    byte_size: int
    storage_path: str
    public_url: str


@dataclass
class AssetRecord:
    sha256: str
    original_filename: str
    content_type: str
    file_ext: str
    byte_size: int
    width: int
    height: int
    storage_path: str
    public_url: str


@dataclass
class AssetLookup:
    asset_id: int
    sha256: str
    storage_path: str
    public_url: str
    status: str
    variants: List[VariantRecord]


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _psql_base_command(self) -> list[str]:
        command = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t"]
        if self.settings.pg_database:
            command.append(self.settings.pg_database)
        return command

    def run_sql(self, sql: str) -> str:
        completed = subprocess.run(
            self._psql_base_command() + ["-c", sql],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def apply_schema(self, schema_path: Path) -> None:
        subprocess.run(
            self._psql_base_command() + ["-f", str(schema_path)],
            check=True,
            capture_output=True,
            text=True,
        )

    def insert_asset(self, asset: AssetRecord, variants: Iterable[VariantRecord]) -> int:
        sql = f"""
WITH inserted AS (
    INSERT INTO assets (
        sha256, original_filename, content_type, file_ext, byte_size, width, height, storage_path, public_url
    ) VALUES (
        {_sql_literal(asset.sha256)},
        {_sql_literal(asset.original_filename)},
        {_sql_literal(asset.content_type)},
        {_sql_literal(asset.file_ext)},
        {_sql_literal(asset.byte_size)},
        {_sql_literal(asset.width)},
        {_sql_literal(asset.height)},
        {_sql_literal(asset.storage_path)},
        {_sql_literal(asset.public_url)}
    )
    ON CONFLICT (sha256) DO UPDATE
    SET
        original_filename = EXCLUDED.original_filename,
        content_type = EXCLUDED.content_type,
        file_ext = EXCLUDED.file_ext,
        byte_size = EXCLUDED.byte_size,
        width = EXCLUDED.width,
        height = EXCLUDED.height,
        storage_path = EXCLUDED.storage_path,
        public_url = EXCLUDED.public_url,
        status = 'active',
        deleted_at = NULL
    RETURNING id
)
SELECT id FROM inserted;
"""
        raw = self.run_sql(sql)
        asset_id = int(raw.splitlines()[-1])
        for variant in variants:
            self.insert_variant(asset_id, variant)
        return asset_id

    def insert_variant(self, asset_id: int, variant: VariantRecord) -> None:
        sql = f"""
INSERT INTO asset_variants (
    asset_id, kind, format, width, height, byte_size, storage_path, public_url
) VALUES (
    {_sql_literal(asset_id)},
    {_sql_literal(variant.kind)},
    {_sql_literal(variant.format)},
    {_sql_literal(variant.width)},
    {_sql_literal(variant.height)},
    {_sql_literal(variant.byte_size)},
    {_sql_literal(variant.storage_path)},
    {_sql_literal(variant.public_url)}
)
ON CONFLICT (asset_id, kind) DO UPDATE
SET
    format = EXCLUDED.format,
    width = EXCLUDED.width,
    height = EXCLUDED.height,
    byte_size = EXCLUDED.byte_size,
    storage_path = EXCLUDED.storage_path,
    public_url = EXCLUDED.public_url,
    deleted_at = NULL;
"""
        self.run_sql(sql)

    def find_asset(self, sha256: str) -> Optional[AssetLookup]:
        sql = f"""
SELECT json_build_object(
    'asset_id', a.id,
    'sha256', a.sha256,
    'storage_path', a.storage_path,
    'public_url', a.public_url,
    'status', a.status,
    'variants', COALESCE(
        (
            SELECT json_agg(
                json_build_object(
                    'kind', v.kind,
                    'format', v.format,
                    'width', v.width,
                    'height', v.height,
                    'byte_size', v.byte_size,
                    'storage_path', v.storage_path,
                    'public_url', v.public_url
                )
                ORDER BY v.width
            )
            FROM asset_variants v
            WHERE v.asset_id = a.id AND v.deleted_at IS NULL
        ),
        '[]'::json
    )
)
FROM assets a
WHERE a.sha256 = {_sql_literal(sha256)}
LIMIT 1;
"""
        raw = self.run_sql(sql)
        if not raw:
            return None
        payload = json.loads(raw)
        variants = [VariantRecord(**variant) for variant in payload["variants"]]
        return AssetLookup(
            asset_id=payload["asset_id"],
            sha256=payload["sha256"],
            storage_path=payload["storage_path"],
            public_url=payload["public_url"],
            status=payload["status"],
            variants=variants,
        )

    def mark_deleted(self, sha256: str) -> None:
        sql = f"""
UPDATE asset_variants
SET deleted_at = NOW()
WHERE asset_id = (SELECT id FROM assets WHERE sha256 = {_sql_literal(sha256)});

UPDATE assets
SET status = 'deleted', deleted_at = NOW()
WHERE sha256 = {_sql_literal(sha256)};
"""
        self.run_sql(sql)
