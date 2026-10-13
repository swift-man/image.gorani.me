"""서비스가 psql 로 보내는 모든 SQL 이 search_path 를 고정하고 테이블을 public. 으로 가리키는지 확인한다.

왜 중요한가: API migration 역할은 schema public 에 함수·연산자를 만들 수 있다(운영에서 아직은 슈퍼유저라
잠복 상태지만, 역할을 쪼개면 드러난다). 고정하지 않으면 형변환이 필요한 호출을 타입이 정확히 맞는 그 역할의
오버로드가 가로채, 이 psql 을 돌린 역할(운영은 슈퍼유저)의 권한으로 돈다. 대표 벡터:
- _sql_literal 의 convert_from(decode('…','hex'), 'UTF8') — 타입 없는 'UTF8' 때문에 심은
  public.convert_from(bytea, text) 가 pg_catalog 의 (bytea, name) 보다 먼저 골라진다(모든 문자열 리터럴에서).
- find_asset 의 a.sha256 = convert_from(…) — char(64) = text 비교라 심은 public.=(character, text) 가 골라진다.

search_path 를 pg_catalog, pg_temp 로 고정하면(plain SET, public 없음) 이 오버로드는 qualification 없는 호출의
후보가 되지 못한다. 테이블은 public 에 있어 public. 으로 가리킨다. 통합 시험은 폐기용 클러스터에서 이를 증명한다:
public 에 CREATE 만 있는 역할이 오버로드를 심고, 고정을 뺀 대조는 그 오버로드를 슈퍼유저로 부르며, 실제 코드는
하나도 부르지 않는다.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upload_service import fix_storage_root
from upload_service.config import PostgreSQLSettings
from upload_service.db import (
    SEARCH_PATH_PIN,
    AssetRecord,
    Database,
    VariantRecord,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

# 서비스가 쓰는 영구 테이블. 모두 public 에 있다.
TABLE_NAMES = ("assets", "asset_variants", "pending_file_deletions")

DIGEST = "a" * 64


def _strip_sql_noise(sql: str) -> str:
    """-- 주석과 작은따옴표 문자열을 지운다. 테이블 이름이 문자열·주석 안에 있으면(to_regclass('public.assets'))
    qualification 검사에서 빼기 위함이다."""
    code = re.sub(r"--[^\n]*", "", sql)
    code = re.sub(r"'(?:[^']|'')*'", "''", code)
    return code


def bare_table_references(sql: str) -> list[str]:
    """public. 없이(또는 다른 스키마 없이) 쓴 테이블 이름. 앞에 '.' 이나 단어 문자가 있으면(public.assets,
    asset_sha256) 테이블 참조가 아니다."""
    code = _strip_sql_noise(sql)
    found = []
    for name in TABLE_NAMES:
        for match in re.finditer(rf"(?<![.\w])({name})\b(?!\s*\.)", code):
            found.append(f"{name} @ offset {match.start()}")
    return found


class _CapturingDatabase(Database):
    """psql 을 부르지 않고 run_sql/apply_schema 가 만드는 명령을 받아 둔다. canned stdout 으로 상위 메서드가
    결과를 파싱할 수 있게 한다."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.commands: list[list[str]] = []
        self.stdout = ""

    def _run_psql(self, command, operation_name):  # type: ignore[override]
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=self.stdout, stderr="")

    @property
    def last_payload(self) -> str:
        command = self.commands[-1]
        return command[command.index("-c") + 1]


def _settings() -> PostgreSQLSettings:
    return PostgreSQLSettings(database_url=None, pg_database="postgres", db_timeout_seconds=10)


def _asset(sha256: str = DIGEST) -> AssetRecord:
    return AssetRecord(
        sha256=sha256,
        original_filename="005930.png",
        content_type="image/png",
        file_ext=".png",
        byte_size=100,
        width=10,
        height=10,
        storage_path="/root/original/aa/aa/logo.png",
        public_url="/i/original/aa/aa/logo.png",
    )


def _variant(storage_path: str) -> VariantRecord:
    return VariantRecord(
        kind="thumb_64",
        format="webp",
        width=64,
        height=32,
        byte_size=10,
        storage_path=storage_path,
        public_url="/i/variants/aa/aa/logo__thumb_64.webp",
    )


class PinContractTests(unittest.TestCase):
    """run_sql/apply_schema 가 고정 문장을 앞에 붙이고 -q 로 돈다."""

    def test_run_sql_prepends_the_pin_and_runs_quietly(self) -> None:
        database = _CapturingDatabase(_settings())
        database.run_sql("SELECT 1;")
        command = database.commands[-1]
        self.assertEqual(command[-3:], ["-q", "-c", f"{SEARCH_PATH_PIN}\nSELECT 1;"])
        # 고정은 plain SET 이어야 BEGIN…COMMIT 안과 그 뒤 문장까지 남는다.
        self.assertNotIn("SET LOCAL", command[-1])
        self.assertTrue(command[-1].startswith(SEARCH_PATH_PIN + "\n"))

    def test_apply_schema_runs_the_file_quietly(self) -> None:
        database = _CapturingDatabase(_settings())
        database.apply_schema(Path("/tmp/schema.sql"))
        command = database.commands[-1]
        self.assertEqual(command[-3:], ["-q", "-f", "/tmp/schema.sql"])

    def test_schema_file_pins_first_and_qualifies_every_object(self) -> None:
        text = SCHEMA_PATH.read_text(encoding="utf-8")
        # 주석을 뗀 첫 문장이 고정이어야 한다. 고정 앞에 다른 문장이 끼면 그 문장이 고정 없이 돈다.
        without_comments = re.sub(r"--[^\n]*", "", text)
        statements = [" ".join(part.split()) for part in without_comments.split(";") if part.strip()]
        self.assertEqual(statements[0], SEARCH_PATH_PIN.rstrip(";"))
        self.assertEqual(bare_table_references(text), [])
        # CREATE/ALTER 대상이 모두 public. 이다(고정 경로에선 기본 생성 스키마가 pg_catalog 라 필수).
        for match in re.finditer(r"(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE)\s+(\S+)", text, re.I):
            self.assertTrue(match.group(1).startswith("public."), match.group(0))
        for match in re.finditer(r"CREATE\s+INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+\w+\s+ON\s+(\S+)", text, re.I):
            self.assertTrue(match.group(1).startswith("public."), match.group(0))


class QualificationTests(unittest.TestCase):
    """메서드가 만드는 SQL 이 테이블을 public. 으로 가리키고, 고정 문장을 먼저 보낸다."""

    def _payload_for(self, call, stdout: str = "") -> str:
        database = _CapturingDatabase(_settings())
        database.stdout = stdout
        call(database)
        payload = database.last_payload
        self.assertTrue(payload.startswith(SEARCH_PATH_PIN + "\n"), payload)
        body = payload[len(SEARCH_PATH_PIN):]
        self.assertEqual(bare_table_references(body), [], body)
        return body

    def test_insert_asset(self) -> None:
        body = self._payload_for(
            lambda db: db.insert_asset(_asset(), [_variant("/root/variants/aa/aa/logo__thumb_64.webp")]),
            stdout="42",
        )
        self.assertIn("INSERT INTO public.assets", body)
        self.assertIn("INSERT INTO public.asset_variants", body)
        self.assertIn("INSERT INTO public.pending_file_deletions", body)
        self.assertIn("UPDATE public.asset_variants", body)

    def test_find_asset(self) -> None:
        body = self._payload_for(lambda db: db.find_asset(DIGEST), stdout="")
        self.assertIn("FROM public.assets a", body)
        self.assertIn("FROM public.asset_variants v", body)

    def test_find_pending_file_deletions(self) -> None:
        body = self._payload_for(lambda db: db.find_pending_file_deletions(100), stdout="[]")
        self.assertIn("FROM public.pending_file_deletions queue", body)
        self.assertIn("FROM public.assets asset", body)
        self.assertIn("FROM public.asset_variants variant", body)

    def test_is_file_deletion_ready(self) -> None:
        body = self._payload_for(lambda db: db.is_file_deletion_ready(1), stdout="f")
        self.assertIn("FROM public.pending_file_deletions queue", body)

    def test_mark_file_deletion_completed(self) -> None:
        body = self._payload_for(lambda db: db.mark_file_deletion_completed(1))
        self.assertIn("UPDATE public.pending_file_deletions", body)

    def test_mark_deleting(self) -> None:
        body = self._payload_for(lambda db: db.mark_deleting(DIGEST))
        self.assertIn("UPDATE public.assets", body)

    def test_mark_deleted(self) -> None:
        body = self._payload_for(lambda db: db.mark_deleted(DIGEST))
        self.assertIn("UPDATE public.asset_variants", body)
        self.assertIn("FROM public.assets WHERE", body)
        self.assertIn("UPDATE public.assets", body)
        # BEGIN…COMMIT 가 고정 뒤에 오고, 고정은 plain SET 이라 그 안에서도 유지된다.
        self.assertTrue(body.lstrip().startswith("BEGIN;"))
        self.assertIn("COMMIT;", body)

    def test_update_storage_root(self) -> None:
        body = self._payload_for(
            lambda db: fix_storage_root.update_storage_root(db, "/root", "/newroot")
        )
        self.assertIn("to_regclass('public.assets')", body)
        self.assertIn("to_regclass('public.asset_variants')", body)
        self.assertIn("to_regclass('public.pending_file_deletions')", body)
        self.assertIn("UPDATE public.assets", body)
        self.assertIn("UPDATE public.asset_variants", body)
        self.assertIn("UPDATE public.pending_file_deletions", body)


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        f"/opt/homebrew/opt/postgresql@17/bin/{name}",
        f"/usr/local/opt/postgresql@17/bin/{name}",
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
    ):
        if Path(candidate).exists():
            return candidate
    return None


POSTGRES_BINARIES = {name: _find_binary(name) for name in ("initdb", "pg_ctl", "psql")}
POSTGRES_AVAILABLE = all(POSTGRES_BINARIES.values())

# public 에 CREATE 만 있는 역할(운영의 API migration 역할처럼). 오버로드를 심어 두면, 고정 없이 도는 세션(슈퍼유저)이
# 형변환 호출에서 이 역할의 함수·연산자를 부른다. 함수는 호출자 권한(SECURITY INVOKER)으로 돌아, 부른 값을 기록한다.
ATTACKER_ROLE = "image_sp_attacker"
PLANTED_OVERLOADS_SQL = """
CREATE TABLE IF NOT EXISTS public.planted_calls (signature text NOT NULL, called_as name NOT NULL);
GRANT INSERT ON public.planted_calls TO PUBLIC;
CREATE FUNCTION public.convert_from(bytea, text) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO public.planted_calls VALUES ('convert_from(bytea,text)', current_user);
    RETURN pg_catalog.convert_from($1, $2::name);
END $$;
CREATE FUNCTION public.image_sp_char_text_eq(character, text) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO public.planted_calls VALUES ('=(character,text)', current_user);
    RETURN $1::text OPERATOR(pg_catalog.=) $2;
END $$;
CREATE OPERATOR public.= (LEFTARG = character, RIGHTARG = text, FUNCTION = public.image_sp_char_text_eq);
"""


@unittest.skipUnless(
    POSTGRES_AVAILABLE,
    "local PostgreSQL server binaries (initdb/pg_ctl/psql) are required for the integration test",
)
class PlantedOverloadIntegrationTests(unittest.TestCase):
    """폐기용 클러스터에서 고정의 효과를 A/B 로 증명한다. 운영 PostgreSQL 에는 절대 붙지 않는다:
    127.0.0.1 의 5432 가 아닌 임의 포트에 initdb 로 띄우고 unix 소켓을 끈다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.superuser = getpass.getuser()
        cls.temporary_directory = tempfile.TemporaryDirectory(prefix="image-sp-pg-", dir="/tmp")
        cls.root = Path(cls.temporary_directory.name)
        cls.pgdata = cls.root / "pgdata"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        assert cls.port != 5432  # 운영 포트는 절대 쓰지 않는다.
        cls.database = "image_sp"
        try:
            cls._run(
                [
                    POSTGRES_BINARIES["initdb"], "-D", str(cls.pgdata),
                    "--no-locale", "--encoding=UTF8",
                    "--auth-local=trust", "--auth-host=trust",
                ]
            )
            cls._run(
                [
                    POSTGRES_BINARIES["pg_ctl"], "-D", str(cls.pgdata),
                    "-l", str(cls.root / "postgres.log"),
                    "-o", f"-p {cls.port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''",
                    "-w", "start",
                ]
            )
            cls.server_started = True
            cls._run(cls._admin("postgres") + ["-c", f'CREATE DATABASE "{cls.database}";'])
            # 실제 apply_schema 경로로 스키마를 적용한다(고정 + public. 적용까지 함께 확인).
            Database(cls._service_settings()).apply_schema(SCHEMA_PATH)
            cls._seed()
            cls._run(
                cls._admin(cls.database)
                + [
                    "-c",
                    f"CREATE ROLE {ATTACKER_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; "
                    f"GRANT CREATE ON SCHEMA public TO {ATTACKER_ROLE};",
                ]
            )
            cls._run(
                cls._admin(cls.database)
                + ["--single-transaction", "-c", f"SET ROLE {ATTACKER_ROLE}", "-c", PLANTED_OVERLOADS_SQL]
            )
        except BaseException as error:
            log = cls.root / "postgres.log"
            tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
            try:
                cls._stop_server()
            finally:
                cls.temporary_directory.cleanup()
            raise RuntimeError(f"{error}\npostgres.log:\n{tail}") from error

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_server()
        cls.temporary_directory.cleanup()

    @classmethod
    def _stop_server(cls) -> None:
        if getattr(cls, "server_started", False):
            cls._run(
                [POSTGRES_BINARIES["pg_ctl"], "-D", str(cls.pgdata), "-m", "fast", "-w", "stop"],
                check=False,
            )
            cls.server_started = False

    @classmethod
    def _service_settings(cls) -> PostgreSQLSettings:
        # 이미지 서비스 Database 가 쓰는 접속 설정. 접속 대상·역할을 URL 에 명확히 적어, 러너의 PG* 와 무관하게
        # 폐기용 클러스터의 슈퍼유저로만 붙는다.
        url = f"postgresql://{cls.superuser}@127.0.0.1:{cls.port}/{cls.database}"
        return PostgreSQLSettings(database_url=url, pg_database=None, db_timeout_seconds=10)

    @classmethod
    def _admin(cls, database: str) -> list[str]:
        return [
            POSTGRES_BINARIES["psql"], "-X", "-v", "ON_ERROR_STOP=1", "-q",
            "-h", "127.0.0.1", "-p", str(cls.port), "-U", cls.superuser, "-d", database,
        ]

    @staticmethod
    def _environment() -> dict:
        # 러너 셸의 PG*/DATABASE_URL 은 폐기용 서버와 맞지 않으므로 넘기지 않는다.
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PG") and name not in ("DATABASE_URL", "PIPELINE_DATABASE_URL")
        }
        environment.update({"LC_ALL": "C", "PGCLIENTENCODING": "UTF8"})
        return environment

    @classmethod
    def _run(cls, command, *, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(part) for part in command],
            capture_output=True, encoding="utf-8", env=cls._environment(),
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"command failed ({result.returncode}): {' '.join(str(p) for p in command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    @classmethod
    def _seed(cls) -> None:
        cls._run(
            cls._admin(cls.database)
            + [
                "-c",
                f"""
                INSERT INTO public.assets
                    (sha256, original_filename, content_type, file_ext, byte_size, width, height, storage_path, public_url)
                VALUES ('{DIGEST}', 'seed.png', 'image/png', '.png', 100, 10, 10,
                        '/root/original/aa/aa/seed.png', '/i/original/aa/aa/seed.png');
                INSERT INTO public.asset_variants
                    (asset_id, kind, format, width, height, byte_size, storage_path, public_url)
                VALUES ((SELECT id FROM public.assets WHERE sha256 = '{DIGEST}'),
                        'thumb_64', 'webp', 64, 32, 10,
                        '/root/variants/aa/aa/seed__thumb_64.webp', '/i/variants/aa/aa/seed__thumb_64.webp');
                INSERT INTO public.pending_file_deletions (storage_path, asset_sha256, reason)
                VALUES ('/root/variants/aa/aa/orphan.webp', '{DIGEST}', 'variant_replaced');
                """,
            ]
        )

    def _planted_calls(self) -> list:
        raw = self._run(
            self._admin(self.database)
            + ["-At", "-c", "SELECT COALESCE(json_agg(json_build_array(signature, called_as)), '[]') FROM public.planted_calls;"]
        ).stdout.strip()
        return json.loads(raw)

    def _truncate_planted_calls(self) -> None:
        self._run(self._admin(self.database) + ["-c", "TRUNCATE public.planted_calls;"])

    def _pending_id(self) -> int:
        raw = self._run(
            self._admin(self.database)
            + ["-At", "-c", "SELECT id FROM public.pending_file_deletions ORDER BY id LIMIT 1;"]
        ).stdout.strip()
        return int(raw)

    def test_the_superuser_can_run_planted_code(self) -> None:
        # 대조와 실제 실행이 모두 슈퍼유저로 붙는지 전제를 확인한다(아니면 증명이 무의미하다).
        raw = self._run(
            self._admin(self.database)
            + ["-At", "-c", "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user;"]
        ).stdout.strip()
        user, _, is_super = raw.partition("|")
        self.assertEqual(user, self.superuser)
        self.assertEqual(is_super, "t")

    def _drive_every_method(self, database: Database) -> None:
        pending_id = self._pending_id()
        self.assertIsNotNone(database.find_asset(DIGEST))
        database.insert_asset(_asset(), [_variant("/root/variants/aa/aa/seed__thumb_64_new.webp")])
        database.find_pending_file_deletions(100)
        database.is_file_deletion_ready(pending_id)
        database.mark_file_deletion_completed(pending_id)
        database.mark_deleting(DIGEST)
        database.mark_deleted(DIGEST)
        fix_storage_root.update_storage_root(database, "/root", "/newroot")

    def test_pinned_service_sql_calls_no_planted_overload(self) -> None:
        self._truncate_planted_calls()

        class _Recording(Database):
            payloads: list = []

            def _run_psql(self, command, operation_name):  # type: ignore[override]
                if "-c" in command:
                    _Recording.payloads.append(command[command.index("-c") + 1])
                return super()._run_psql(command, operation_name)

        _Recording.payloads = []
        self._drive_every_method(_Recording(self._service_settings()))

        self.assertEqual(self._planted_calls(), [], "고정한 실제 SQL 이 심은 오버로드를 불렀다")
        self.assertTrue(_Recording.payloads)
        for payload in _Recording.payloads:
            self.assertTrue(payload.startswith(SEARCH_PATH_PIN + "\n"), payload)

        # 같은 SQL 에서 고정만 빼면(대조) 슈퍼유저로 심은 오버로드가 불린다 — 증명이 공허하지 않음을 보인다.
        self._truncate_planted_calls()
        for payload in _Recording.payloads:
            self.assertTrue(payload.startswith(SEARCH_PATH_PIN + "\n"))
            unpinned = payload[len(SEARCH_PATH_PIN):]
            self._run(self._admin(self.database) + ["-c", unpinned])

        calls = self._planted_calls()
        self.assertTrue(calls, "고정을 뺀 대조가 심은 오버로드를 하나도 부르지 않았다(시험이 무의미)")
        signatures = {signature for signature, _ in calls}
        self.assertIn("convert_from(bytea,text)", signatures)
        self.assertIn("=(character,text)", signatures)
        self.assertTrue(all(called_as == self.superuser for _, called_as in calls), calls)


if __name__ == "__main__":
    unittest.main()
