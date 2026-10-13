"""PostgreSQL에 보내는 SQL이 search_path를 고정하고 표를 public.으로 쓰는지 확인한다.

public에 함수·연산자를 만들 수 있는 역할(CREATE ON SCHEMA public만 가진 역할)이 인자 타입이 정확히
맞는 오버로드를 심으면, 고정하지 않은 SQL은 형변환이 필요한 호출(convert_from(bytea, 'UTF8'),
char(64) = text 비교, json_build_object, json_agg)에서 그 오버로드를 서비스가 접속한 역할(슈퍼유저일
수 있다)의 권한으로 부른다.

통합 시험은 /tmp 아래에 임시 클러스터를 띄운다. TCP를 열지 않고 전용 디렉터리의 유닉스 소켓만 쓴다.
기본 libpq 대상(기본 소켓·5432 포트)에는 운영 DB가 있을 수 있으므로 PG*·DATABASE_URL 환경변수를
지우고 모든 접속에 소켓 디렉터리와 포트를 명시한다. initdb·pg_ctl·psql이 없으면 건너뛴다.
"""

from __future__ import annotations

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

from upload_service import db as db_module
from upload_service.config import PostgreSQLSettings
from upload_service.db import SEARCH_PATH_PIN, AssetRecord, Database, VariantRecord
from upload_service.fix_storage_root import update_storage_root


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"
# public. 없이 쓴 표 이름. public.assets, 열 이름(asset_sha256), 인덱스 이름(idx_assets_status)은
# 걸리지 않는다.
BARE_TABLE = re.compile(r"(?<![.\w])(?:assets|asset_variants|pending_file_deletions)\b")
POSTGRES_BINARIES = {name: shutil.which(name) for name in ("initdb", "pg_ctl", "psql")}
# PG* 말고 접속 대상을 담는 환경변수(같은 DB를 쓰는 배치의 이름 포함). PG*와 함께 지운다.
CONNECTION_VARIABLES = ("DATABASE_URL", "PIPELINE_DATABASE_URL")

SHA256 = "ab" * 32
ASSET = AssetRecord(
    sha256=SHA256,
    original_filename="로고.png",
    content_type="image/png",
    file_ext=".png",
    byte_size=100,
    width=200,
    height=100,
    storage_path=f"/store/original/ab/ab/{SHA256}.png",
    public_url=f"/i/original/ab/ab/{SHA256}.png",
)


def make_variant(width: int, image_format: str) -> VariantRecord:
    name = f"{SHA256}__thumb_{width}.{image_format}"
    return VariantRecord(
        kind=f"thumb_{width}",
        format=image_format,
        width=width,
        height=width // 2,
        byte_size=width // 8,
        storage_path=f"/store/variants/ab/ab/{name}",
        public_url=f"/i/variants/ab/ab/{name}",
    )


def without_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def database_settings(pg_database: str = "postgres") -> PostgreSQLSettings:
    return PostgreSQLSettings(database_url=None, pg_database=pg_database, db_timeout_seconds=10)


def file_names(pending: list) -> list:
    return [Path(item.storage_path).name for item in pending]


def run_every_statement(database: Database) -> dict:
    """Database의 모든 메서드와 저장 경로 보정을 차례로 실행하고 결과를 모은다."""
    result = {}
    result["inserted"] = database.insert_asset(
        ASSET, [make_variant(160, "png"), make_variant(320, "png")]
    )
    found = database.find_asset(SHA256)
    result["found"] = (found.original_filename, [variant.kind for variant in found.variants])
    # 160 썸네일은 다른 파일로 바뀌고 320 썸네일은 빠져 두 파일이 삭제 큐에 들어간다.
    database.insert_asset(ASSET, [make_variant(160, "webp")])
    pending = database.find_pending_file_deletions(10)
    result["pending"] = file_names(pending)
    result["ready"] = [database.is_file_deletion_ready(item.deletion_id) for item in pending]
    database.mark_file_deletion_completed(pending[0].deletion_id)
    result["remaining"] = file_names(database.find_pending_file_deletions(10))
    database.mark_deleting(SHA256)
    result["deleting"] = database.find_asset(SHA256).status
    database.mark_deleted(SHA256)
    deleted = database.find_asset(SHA256)
    result["deleted"] = (deleted.status, deleted.variants)
    update_storage_root(database, "/store", "/Volumes/gorani-images/image-store")
    result["moved"] = database.find_asset(SHA256).storage_path
    return result


class SearchPathStatementTests(unittest.TestCase):
    def test_run_sql_pins_search_path_in_the_same_quiet_psql_command(self) -> None:
        # SET LOCAL이나 public이 든 경로는 고정이 아니다.
        self.assertEqual(SEARCH_PATH_PIN, "SET search_path = pg_catalog, pg_temp;")
        database = Database(database_settings())
        completed = subprocess.CompletedProcess(["psql"], 0, stdout="42\n", stderr="")

        with patch("upload_service.db.subprocess.run", return_value=completed) as run:
            self.assertEqual(database.run_sql("SELECT 42;"), "42")

        command = run.call_args.args[0]
        base_command = database._psql_base_command()
        self.assertEqual(command[: len(base_command)], base_command)
        self.assertEqual(
            command[len(base_command):],
            ["-q", "-c", f"{SEARCH_PATH_PIN}\nSELECT 42;"],
        )

    def test_every_statement_names_its_tables_with_public(self) -> None:
        lookup = {
            "asset_id": 7,
            "sha256": SHA256,
            "original_filename": "로고.png",
            "content_type": "image/png",
            "file_ext": ".png",
            "byte_size": 100,
            "width": 200,
            "height": 100,
            "storage_path": ASSET.storage_path,
            "public_url": ASSET.public_url,
            "status": "active",
            "variants": [],
        }
        calls = {
            "insert_asset": (
                "7",
                lambda database: database.insert_asset(ASSET, [make_variant(160, "webp")]),
            ),
            "insert_asset without variants": (
                "7",
                lambda database: database.insert_asset(ASSET, []),
            ),
            "find_asset": (json.dumps(lookup), lambda database: database.find_asset(SHA256)),
            "find_pending_file_deletions": (
                "[]",
                lambda database: database.find_pending_file_deletions(10),
            ),
            "is_file_deletion_ready": ("t", lambda database: database.is_file_deletion_ready(3)),
            "mark_file_deletion_completed": (
                "",
                lambda database: database.mark_file_deletion_completed(3),
            ),
            "mark_deleting": ("", lambda database: database.mark_deleting(SHA256)),
            "mark_deleted": ("", lambda database: database.mark_deleted(SHA256)),
            "update_storage_root": (
                "",
                lambda database: update_storage_root(database, "/old", "/new"),
            ),
        }
        for name, (stdout, call) in calls.items():
            with self.subTest(name):
                database = Database(database_settings())
                completed = subprocess.CompletedProcess(["psql"], 0, stdout=stdout, stderr="")
                with patch("upload_service.db.subprocess.run", return_value=completed) as run:
                    call(database)

                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertIn("-q", command)
                self.assertEqual(command[-2], "-c")
                sql = command[-1]
                self.assertTrue(sql.startswith(SEARCH_PATH_PIN + "\n"), sql)
                body = without_comments(sql[len(SEARCH_PATH_PIN):])
                self.assertIn("public.", body)
                self.assertEqual(BARE_TABLE.findall(body), [])
                self.assertNotIn("search_path", body)

    def test_schema_pins_search_path_before_any_object_is_created(self) -> None:
        schema = without_comments(SCHEMA_PATH.read_text(encoding="utf-8"))
        statements = [" ".join(part.split()) for part in schema.split(";") if part.strip()]

        self.assertEqual(statements[0] + ";", SEARCH_PATH_PIN)
        self.assertNotIn("search_path", " ".join(statements[1:]))
        self.assertEqual(BARE_TABLE.findall(schema), [])
        for statement in statements[1:]:
            with self.subTest(statement=statement[:60]):
                self.assertIn("public.", statement)


@unittest.skipIf(
    None in POSTGRES_BINARIES.values(),
    "PostgreSQL binaries not found: initdb, pg_ctl, psql",
)
class PostgreSQLSearchPathTests(unittest.TestCase):
    """실제 PostgreSQL에서 SQL이 고정한 경로로 실행되고 public에 심은 오버로드를 부르지 않는지 본다."""

    database_name = "image_search_path"
    superuser = "image_superuser"
    attacker = "planted_migration"
    # json_build_object 호출을 겨누는 오버로드의 인자 타입. find_asset과 find_pending_file_deletions가
    # 넘기는 순서 그대로다. SQL을 바꾸면 함께 고친다(대조가 그 오버로드를 부르지 않아 실패한다).
    json_build_object_plants = {
        "json_build_object(find_asset)": (
            "text, bigint, text, character, text, text, text, text, text, text, text, bigint,"
            " text, integer, text, integer, text, text, text, text, text, text, text, json"
        ),
        "json_build_object(find_asset variants)": (
            "text, text, text, text, text, integer, text, integer, text, bigint,"
            " text, text, text, text"
        ),
        "json_build_object(find_pending_file_deletions)": (
            "text, bigint, text, text, text, character"
        ),
    }

    @classmethod
    def setUpClass(cls) -> None:
        # 소켓 경로 길이 제한(macOS 104바이트) 때문에 /tmp 아래에 둔다.
        directory = tempfile.TemporaryDirectory(prefix="image-search-path-pg-", dir="/tmp")
        cls.addClassCleanup(directory.cleanup)
        try:
            root = Path(directory.name).resolve()
            cls.socket_directory = root / "socket"
            cls.socket_directory.mkdir()
            cls.data_directory = root / "data"
            # 포트는 전용 디렉터리의 소켓 이름에만 쓰인다. 쓰이지 않는 번호를 고른다.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                cls.port = probe.getsockname()[1]
            cls.admin(
                POSTGRES_BINARIES["initdb"], "-D", cls.data_directory, "-U", cls.superuser,
                "--no-locale", "--encoding=UTF8", "--auth-local=trust", "--auth-host=reject",
            )
            # pg_ctl start가 실패해도 서버가 떠 있을 수 있어 시작 전에 정지를 등록한다.
            # 정리는 역순이라 디렉터리를 지우기 전에 돈다.
            cls.addClassCleanup(cls.stop_server)
            cls.admin(
                POSTGRES_BINARIES["pg_ctl"], "-D", cls.data_directory,
                "-l", root / "postgres.log",
                "-o", f"-k {cls.socket_directory} -p {cls.port} -c listen_addresses=''",
                "-w", "start",
            )
            cls.psql("postgres", f"CREATE DATABASE {cls.database_name};")

            # 시험하는 코드의 psql은 os.environ을 물려받는다. 접속 대상을 바꾸는 변수를 지우고
            # 기본 접속도 임시 클러스터로 향하게 한다.
            environment = cls.environment_without_connection()
            environment.update(PGHOST=str(cls.socket_directory), PGPORT=str(cls.port))
            environment_patch = patch.dict(os.environ, environment, clear=True)
            environment_patch.start()
            cls.addClassCleanup(environment_patch.stop)

            cls.database = Database(
                database_settings(
                    f"host={cls.socket_directory} port={cls.port}"
                    f" dbname={cls.database_name} user={cls.superuser}"
                )
            )
            data_directory = cls.database.run_sql("SELECT current_setting('data_directory');")
            if data_directory != str(cls.data_directory):
                raise RuntimeError(f"not the temporary cluster: {data_directory!r}")
            # 서비스 시작과 같은 경로로 스키마를 두 번 적용한다(IF NOT EXISTS).
            cls.database.apply_schema(SCHEMA_PATH)
            cls.database.apply_schema(SCHEMA_PATH)
            cls.plant_overloads()
        except BaseException:
            # KeyboardInterrupt처럼 Exception 밖의 예외로 끝나면 클래스 정리가 돌지 않아 서버가 남는다.
            cls.doClassCleanups()
            raise

    @staticmethod
    def environment_without_connection() -> dict:
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PG") and key not in CONNECTION_VARIABLES
        }

    @classmethod
    def admin(cls, *command: object) -> str:
        environment = cls.environment_without_connection()
        environment.update(LC_ALL="C", PGCLIENTENCODING="UTF8")
        completed = subprocess.run(
            [str(part) for part in command],
            capture_output=True,
            text=True,
            env=environment,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"command failed ({completed.returncode}): {command}\n{completed.stderr}"
            )
        return completed.stdout

    @classmethod
    def psql(cls, database_name: str, sql: str) -> str:
        return cls.admin(
            POSTGRES_BINARIES["psql"], "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1",
            "-h", cls.socket_directory, "-p", cls.port, "-U", cls.superuser,
            "-d", database_name, "-c", sql,
        ).strip()

    @classmethod
    def stop_server(cls) -> None:
        if (cls.data_directory / "postmaster.pid").exists():
            cls.admin(
                POSTGRES_BINARIES["pg_ctl"], "-D", cls.data_directory, "-m", "fast", "-w", "stop"
            )

    @classmethod
    def plant_overloads(cls) -> None:
        """public에 CREATE만 있는 역할로, 서비스 SQL의 형변환이 필요한 호출을 겨누는 오버로드를 심는다.

        pg_catalog에는 convert_from(bytea, name), =(text, text), json_build_object(VARIADIC "any"),
        json_agg(anyelement)만 있어 인자 타입이 정확히 맞는 public의 오버로드가 먼저 골라진다. 심은
        함수는 부른 역할을 기록하고 원래 값을 돌려줘, 서비스 동작만으로는 가로챈 것을 알 수 없다.
        """
        statements = [
            f"CREATE ROLE {cls.attacker} NOLOGIN;",
            f"GRANT CREATE ON SCHEMA public TO {cls.attacker};",
            f"SET ROLE {cls.attacker};",
            "CREATE TABLE public.planted_calls ("
            " signature text NOT NULL, called_as name NOT NULL, superuser boolean NOT NULL);",
            "CREATE FUNCTION public.planted_call(text) RETURNS void LANGUAGE sql AS $$"
            " INSERT INTO public.planted_calls SELECT $1, current_user, r.rolsuper"
            " FROM pg_catalog.pg_roles r WHERE r.rolname = current_user $$;",
            "CREATE FUNCTION public.convert_from(bytea, text) RETURNS text"
            " LANGUAGE plpgsql AS $$ BEGIN PERFORM public.planted_call('convert_from(bytea, text)');"
            " RETURN pg_catalog.convert_from($1, $2::pg_catalog.name); END $$;",
            "CREATE FUNCTION public.planted_bpchar_eq_text(character, text) RETURNS boolean"
            " LANGUAGE plpgsql AS $$ BEGIN PERFORM public.planted_call('=(character, text)');"
            " RETURN $1::pg_catalog.text OPERATOR(pg_catalog.=) $2; END $$;",
            "CREATE OPERATOR public.= (LEFTARG = character, RIGHTARG = text,"
            " FUNCTION = public.planted_bpchar_eq_text);",
            "CREATE FUNCTION public.planted_json_agg_final(json[]) RETURNS json"
            " LANGUAGE plpgsql AS $$ BEGIN PERFORM public.planted_call('json_agg(json)');"
            " RETURN pg_catalog.array_to_json($1); END $$;",
            "CREATE AGGREGATE public.json_agg(json) (SFUNC = pg_catalog.array_append,"
            " STYPE = json[], FINALFUNC = public.planted_json_agg_final);",
        ]
        for signature, parameters in cls.json_build_object_plants.items():
            arguments = ", ".join(f"${index}" for index in range(1, parameters.count(",") + 2))
            statements.append(
                f"CREATE FUNCTION public.json_build_object({parameters}) RETURNS json"
                f" LANGUAGE plpgsql AS $$ BEGIN PERFORM public.planted_call('{signature}');"
                f" RETURN pg_catalog.json_build_object({arguments}); END $$;"
            )
        cls.psql(cls.database_name, "\n".join(statements))

    def planted_calls(self) -> list:
        rows = self.psql(
            self.database_name,
            "SELECT DISTINCT signature, called_as, superuser FROM public.planted_calls;",
        )
        return sorted(tuple(row.split("|")) for row in rows.splitlines())

    def reset(self) -> None:
        self.psql(
            self.database_name,
            "TRUNCATE public.assets, public.asset_variants, public.pending_file_deletions,"
            " public.planted_calls RESTART IDENTITY;",
        )

    def test_schema_creates_the_metadata_tables_in_public(self) -> None:
        tables = self.psql(
            self.database_name,
            "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c"
            " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r'"
            " AND c.relname IN ('assets', 'asset_variants', 'pending_file_deletions');",
        )

        self.assertEqual(
            sorted(tables.splitlines()),
            ["public|asset_variants", "public|assets", "public|pending_file_deletions"],
        )

    def test_statements_do_not_call_overloads_planted_in_public(self) -> None:
        # 대조가 먼저다. 고정 문장만 뺀 같은 SQL이 심은 오버로드를 슈퍼유저로 부르지 않으면 이 시험은
        # 아무것도 확인하지 못한다.
        is_superuser = "SELECT rolsuper FROM pg_roles WHERE rolname = current_user;"
        self.assertEqual(self.psql(self.database_name, is_superuser), "t")
        planted = ["convert_from(bytea, text)", "=(character, text)", "json_agg(json)"]
        planted += list(self.json_build_object_plants)
        results = {}
        for control in (True, False):
            with self.subTest(control=control):
                self.reset()
                pin = "" if control else SEARCH_PATH_PIN
                with patch.object(db_module, "SEARCH_PATH_PIN", pin):
                    results[control] = run_every_statement(self.database)
                expected = [(signature, self.superuser, "t") for signature in planted]
                self.assertEqual(self.planted_calls(), sorted(expected) if control else [])

        # 심은 함수도 원래 값을 돌려줘 대조와 고정한 실행의 차이는 그 함수를 불렀는지뿐이다.
        self.assertEqual(results[True], results[False])
        self.assertEqual(
            results[False],
            {
                "inserted": 1,
                "found": ("로고.png", ["thumb_160", "thumb_320"]),
                "pending": [f"{SHA256}__thumb_160.png", f"{SHA256}__thumb_320.png"],
                "ready": [True, True],
                "remaining": [f"{SHA256}__thumb_320.png"],
                "deleting": "deleting",
                "deleted": ("deleted", []),
                "moved": f"/Volumes/gorani-images/image-store/original/ab/ab/{SHA256}.png",
            },
        )


if __name__ == "__main__":
    unittest.main()
