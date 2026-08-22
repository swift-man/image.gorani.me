from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .config import PostgreSQLSettings, Settings
from .errors import DatabaseTimeoutError


def _sql_literal(value: object) -> str:
    # 문자열은 UTF-8 16진수로 인코딩해 SQL 구문 문자가 데이터 경계를 벗어나지 못하게 한다.
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    encoded = str(value).encode("utf-8").hex()
    return f"convert_from(decode('{encoded}', 'hex'), 'UTF8')"


@dataclass
class VariantRecord:
    """파생 이미지 한 건을 표현한다."""

    kind: str
    format: str
    width: int
    height: int
    byte_size: int
    storage_path: str
    public_url: str


@dataclass
class AssetRecord:
    """원본 이미지 한 건의 메타데이터를 표현한다."""

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
    """조회 API와 삭제 로직에서 함께 쓰는 합성 조회 결과."""

    asset_id: int
    sha256: str
    storage_path: str
    public_url: str
    status: str
    variants: List[VariantRecord]


@dataclass
class PendingFileDeletion:
    """DB가 추적하며 재시도할 파생 파일 삭제 작업을 표현한다."""

    deletion_id: int
    storage_path: str
    asset_sha256: str | None


class Database:
    """psql CLI를 통해 PostgreSQL과 통신하는 얇은 저장소 계층."""

    def __init__(self, settings: Settings | PostgreSQLSettings) -> None:
        self.settings = settings

    def _psql_base_command(self) -> list[str]:
        # 비밀번호가 들어갈 수 있는 DATABASE_URL은 명령행 인자로 전달하지 않는다.
        command = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t"]
        if self.settings.pg_database and not self.settings.database_url:
            command.append(self.settings.pg_database)
        return command

    def _psql_environment(self) -> dict[str, str]:
        # 접속 URL을 libpq 환경변수로 분해해 프로세스 목록에 비밀번호가 노출되지 않게 한다.
        environment = os.environ.copy()
        environment.pop("DATABASE_URL", None)
        if self.settings.database_url:
            parsed = urlsplit(self.settings.database_url)
            if parsed.hostname:
                environment["PGHOST"] = parsed.hostname
            if parsed.port:
                environment["PGPORT"] = str(parsed.port)
            if parsed.username:
                environment["PGUSER"] = unquote(parsed.username)
            if parsed.password:
                environment["PGPASSWORD"] = unquote(parsed.password)
            if parsed.path and parsed.path != "/":
                environment["PGDATABASE"] = unquote(parsed.path.lstrip("/"))

            option_names = {
                "application_name": "PGAPPNAME",
                "sslcert": "PGSSLCERT",
                "sslkey": "PGSSLKEY",
                "sslmode": "PGSSLMODE",
                "sslrootcert": "PGSSLROOTCERT",
            }
            for name, values in parse_qs(parsed.query).items():
                if name in option_names and values:
                    environment[option_names[name]] = values[-1]

        timeout_seconds = self.settings.db_timeout_seconds
        environment["PGCONNECT_TIMEOUT"] = str(timeout_seconds)
        timeout_options = (
            f"-c statement_timeout={timeout_seconds * 1000} "
            f"-c lock_timeout={timeout_seconds * 1000}"
        )
        existing_options = environment.get("PGOPTIONS", "").strip()
        environment["PGOPTIONS"] = " ".join(
            option for option in (existing_options, timeout_options) if option
        )
        return environment

    def run_sql(self, sql: str) -> str:
        # stdout만 반환해서 단순 조회/업데이트 공통 경로로 재사용한다.
        completed = self._run_psql(
            self._psql_base_command() + ["-c", sql],
            "Database command",
        )
        return completed.stdout.strip()

    def _run_psql(
        self,
        command: list[str],
        operation_name: str,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=self._psql_environment(),
                timeout=self.settings.db_timeout_seconds + 2,
            )
        except subprocess.TimeoutExpired as exc:
            raise DatabaseTimeoutError(
                f"{operation_name} exceeded {self.settings.db_timeout_seconds} seconds"
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").lower()
            if "timeout" in stderr or "timed out" in stderr:
                raise DatabaseTimeoutError(
                    f"{operation_name} exceeded {self.settings.db_timeout_seconds} seconds"
                ) from exc
            raise

    def apply_schema(self, schema_path: Path) -> None:
        # 서버 시작 시 필요한 테이블이 없으면 자동 생성한다.
        self._run_psql(
            self._psql_base_command() + ["-f", str(schema_path)],
            "Database schema command",
        )

    def insert_asset(self, asset: AssetRecord, variants: Iterable[VariantRecord]) -> int:
        # 원본과 모든 파생 메타데이터를 한 SQL 문으로 저장해 부분 커밋을 막는다.
        variant_records = list(variants)
        variant_cte = ""
        variant_join = ""
        if variant_records:
            variant_rows = ",\n        ".join(
                "(" + ", ".join(
                    [
                        _sql_literal(variant.kind),
                        _sql_literal(variant.format),
                        _sql_literal(variant.width),
                        _sql_literal(variant.height),
                        _sql_literal(variant.byte_size),
                        _sql_literal(variant.storage_path),
                        _sql_literal(variant.public_url),
                    ]
                ) + ")"
                for variant in variant_records
            )
            variant_cte = f"""
, variant_input (kind, format, width, height, byte_size, storage_path, public_url) AS (
    VALUES
        {variant_rows}
), upserted_variants AS (
    INSERT INTO asset_variants (
        asset_id, kind, format, width, height, byte_size, storage_path, public_url
    )
    SELECT
        inserted.id,
        variant_input.kind,
        variant_input.format,
        variant_input.width,
        variant_input.height,
        variant_input.byte_size,
        variant_input.storage_path,
        variant_input.public_url
    FROM inserted
    CROSS JOIN variant_input
    ON CONFLICT (asset_id, kind) DO UPDATE
    SET
        format = EXCLUDED.format,
        width = EXCLUDED.width,
        height = EXCLUDED.height,
        byte_size = EXCLUDED.byte_size,
        storage_path = EXCLUDED.storage_path,
        public_url = EXCLUDED.public_url,
        deleted_at = NULL
    RETURNING id
), queued_variant_files AS (
    INSERT INTO pending_file_deletions (storage_path, asset_sha256, reason)
    SELECT
        current_variant.storage_path,
        inserted.sha256,
        'variant_replaced'
    FROM asset_variants current_variant
    JOIN inserted ON inserted.id = current_variant.asset_id
    JOIN variant_input ON variant_input.kind = current_variant.kind
    WHERE current_variant.deleted_at IS NULL
      AND current_variant.storage_path <> variant_input.storage_path
    ON CONFLICT (storage_path) DO UPDATE
    SET
        asset_sha256 = EXCLUDED.asset_sha256,
        reason = EXCLUDED.reason,
        queued_at = NOW(),
        completed_at = NULL
    RETURNING id
)
"""
            variant_join = """
CROSS JOIN (SELECT COUNT(*) FROM upserted_variants) committed_variants
CROSS JOIN (SELECT COUNT(*) FROM queued_variant_files) queued_files
""".strip()

        # 같은 해시가 다시 들어오면 기존 레코드를 active 상태로 되살린다.
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
    RETURNING id, sha256
)
{variant_cte}
SELECT inserted.id
FROM inserted
{variant_join};
"""
        raw = self.run_sql(sql)
        return int(raw.splitlines()[-1])

    def find_asset(self, sha256: str) -> Optional[AssetLookup]:
        # 조회용 JSON을 DB에서 조립해 오면 Python 쪽 매핑이 단순해진다.
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

    def find_pending_file_deletions(self, limit: int = 100) -> List[PendingFileDeletion]:
        # 현재 활성 메타데이터가 참조하지 않는 경로만 반환해 포맷 재전환 경합을 피한다.
        if limit <= 0:
            raise ValueError("Pending deletion limit must be positive")
        sql = f"""
SELECT COALESCE(
    json_agg(
        json_build_object(
            'deletion_id', pending.id,
            'storage_path', pending.storage_path,
            'asset_sha256', pending.asset_sha256
        )
        ORDER BY pending.id
    ),
    '[]'::json
)
FROM (
    SELECT queue.id, queue.storage_path, queue.asset_sha256
    FROM pending_file_deletions queue
    WHERE queue.completed_at IS NULL
      AND NOT EXISTS (
          SELECT 1
          FROM assets asset
          WHERE asset.storage_path = queue.storage_path
            AND asset.status <> 'deleted'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM asset_variants variant
          WHERE variant.storage_path = queue.storage_path
            AND variant.deleted_at IS NULL
      )
    ORDER BY queue.id
    LIMIT {_sql_literal(limit)}
) pending;
"""
        raw = self.run_sql(sql)
        payload = json.loads(raw or "[]")
        return [PendingFileDeletion(**item) for item in payload]

    def is_file_deletion_ready(self, deletion_id: int) -> bool:
        # 잠금을 얻은 뒤 현재 참조 여부를 다시 확인해 오래된 큐 조회 결과를 사용하지 않는다.
        sql = f"""
SELECT EXISTS (
    SELECT 1
    FROM pending_file_deletions queue
    WHERE queue.id = {_sql_literal(deletion_id)}
      AND queue.completed_at IS NULL
      AND NOT EXISTS (
          SELECT 1
          FROM assets asset
          WHERE asset.storage_path = queue.storage_path
            AND asset.status <> 'deleted'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM asset_variants variant
          WHERE variant.storage_path = queue.storage_path
            AND variant.deleted_at IS NULL
      )
);
"""
        return self.run_sql(sql) == "t"

    def mark_file_deletion_completed(self, deletion_id: int) -> None:
        sql = f"""
UPDATE pending_file_deletions
SET completed_at = NOW()
WHERE id = {_sql_literal(deletion_id)};
"""
        self.run_sql(sql)

    def mark_deleting(self, sha256: str) -> None:
        # 파일 작업 전에 중간 상태를 남겨 프로세스가 중단되어도 삭제를 재시도할 수 있게 한다.
        sql = f"""
UPDATE assets
SET status = 'deleting'
WHERE sha256 = {_sql_literal(sha256)}
  AND status IN ('active', 'deleting');
"""
        self.run_sql(sql)

    def mark_deleted(self, sha256: str) -> None:
        # 파일 삭제 이후 DB 상태를 deleted로 바꾸고 deleted_at도 기록한다.
        sql = f"""
BEGIN;
UPDATE asset_variants
SET deleted_at = NOW()
WHERE asset_id = (SELECT id FROM assets WHERE sha256 = {_sql_literal(sha256)});

UPDATE assets
SET status = 'deleted', deleted_at = NOW()
WHERE sha256 = {_sql_literal(sha256)};
COMMIT;
"""
        self.run_sql(sql)
