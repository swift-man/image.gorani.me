from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator
from urllib.parse import parse_qs, urlparse

from python_multipart import parse_form
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import File

from .config import Settings, load_settings
from .db import Database
from .storage import (
    StoredAsset,
    build_asset_record,
    delete_files,
    ensure_storage_roots,
    sha256_file,
    stage_upload,
)


MAX_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
LOGGER = logging.getLogger(__name__)


class _HashLockPool:
    """같은 콘텐츠 해시의 저장과 DB 반영을 한 프로세스 안에서 직렬화한다."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._users: dict[str, int] = {}

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
            self._users[key] = self._users.get(key, 0) + 1
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                self._users[key] -= 1
                if self._users[key] == 0:
                    del self._users[key]
                    del self._locks[key]


class UploadApplication:
    """업로드 서비스의 핵심 유스케이스를 묶는 애플리케이션 계층."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings)
        self._upload_locks = _HashLockPool()

    def ensure_ready(self) -> None:
        # 서버 시작 시 저장소 루트와 DB 스키마를 준비한다.
        ensure_storage_roots(self.settings)
        schema_path = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
        self.db.apply_schema(schema_path)

    def is_authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        # 읽기 API는 공개하고, 쓰기 API만 키 기반으로 보호한다.
        x_api_key = handler.headers.get("X-API-Key", "").strip()
        if x_api_key and x_api_key in self.settings.api_keys:
            return True

        auth_header = handler.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token in self.settings.api_keys:
                return True

        return False

    def _persist_staged_upload(
        self,
        original_filename: str,
        temp_path: Path,
        byte_size: int,
    ) -> tuple[StoredAsset, int]:
        # 같은 해시의 동시 요청이 다른 요청의 롤백 파일을 재사용하지 못하게 묶는다.
        content_sha256 = sha256_file(temp_path)
        with self._upload_locks.hold(content_sha256):
            stored = build_asset_record(
                self.settings,
                original_filename,
                temp_path,
                byte_size,
                content_sha256=content_sha256,
            )
            try:
                asset_id = self.db.insert_asset(stored.asset, stored.variants)
            except Exception:
                delete_files(reversed(stored.created_paths))
                raise
        return stored, asset_id

    def handle_upload(self, request: "UploadRequest") -> tuple[int, Dict[str, Any]]:
        # 업로드는 multipart/form-data만 허용한다.
        content_type = request.handler.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return HTTPStatus.BAD_REQUEST, {"error": "Content-Type must be multipart/form-data"}

        raw_content_length = request.handler.headers.get("Content-Length")
        if raw_content_length is None:
            return HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"}
        try:
            content_length = int(raw_content_length)
        except ValueError:
            return HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"}
        if content_length < 0:
            return HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"}
        max_request_bytes = self.settings.max_upload_bytes + MAX_MULTIPART_OVERHEAD_BYTES
        if content_length > max_request_bytes:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                "error": f"Request exceeds max size of {max_request_bytes} bytes"
            }

        uploaded_files: list[File] = []
        try:
            parse_form(
                {
                    "Content-Type": content_type.encode("latin-1"),
                    "Content-Length": str(content_length).encode("ascii"),
                },
                request.handler.rfile,
                on_field=None,
                on_file=uploaded_files.append,
            )
        except FormParserError:
            for uploaded_file in uploaded_files:
                uploaded_file.close()
            return HTTPStatus.BAD_REQUEST, {"error": "Malformed multipart body"}
        except Exception:
            for uploaded_file in uploaded_files:
                uploaded_file.close()
            LOGGER.exception("Multipart parsing failed")
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"}

        image_files = [item for item in uploaded_files if item.field_name == b"image"]
        if len(image_files) != 1:
            for uploaded_file in uploaded_files:
                uploaded_file.close()
            return HTTPStatus.BAD_REQUEST, {"error": "Missing form field: image"}

        file_item = image_files[0]
        original_filename = (file_item.file_name or b"upload").decode("utf-8", errors="replace")
        file_item.file_object.seek(0)
        temp_path = None
        try:
            # 먼저 임시 파일로 받은 뒤, 검사와 저장을 진행한다.
            temp_path, byte_size = stage_upload(
                file_item.file_object,
                original_filename,
                self.settings.max_upload_bytes,
            )
            stored, asset_id = self._persist_staged_upload(original_filename, temp_path, byte_size)
        except ValueError as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            LOGGER.exception("Upload failed")
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"}
        finally:
            for uploaded_file in uploaded_files:
                uploaded_file.close()

        return HTTPStatus.CREATED, {
            "id": asset_id,
            "sha256": stored.asset.sha256,
            "original": {
                "url": stored.asset.public_url,
                "content_type": stored.asset.content_type,
                "width": stored.asset.width,
                "height": stored.asset.height,
                "bytes": stored.asset.byte_size,
            },
            "variants": [
                {
                    "kind": variant.kind,
                    "url": variant.public_url,
                    "width": variant.width,
                    "height": variant.height,
                    "bytes": variant.byte_size,
                }
                for variant in stored.variants
            ],
        }

    def handle_delete(self, sha256: str) -> tuple[int, Dict[str, Any]]:
        # 삭제 중 상태를 먼저 기록하고 같은 해시의 업로드와 직렬화한다.
        try:
            with self._upload_locks.hold(sha256):
                asset = self.db.find_asset(sha256)
                if asset is None:
                    return HTTPStatus.NOT_FOUND, {"error": "Asset not found"}
                if asset.status == "deleted":
                    return HTTPStatus.OK, {"status": "deleted", "sha256": sha256}

                self.db.mark_deleting(sha256)
                delete_files([variant.storage_path for variant in asset.variants] + [asset.storage_path])
                self.db.mark_deleted(sha256)
            return HTTPStatus.OK, {"status": "deleted", "sha256": sha256}
        except Exception:
            # 파일 삭제는 missing_ok로 재시도 가능하므로 다음 DELETE 요청에서 복구할 수 있다.
            LOGGER.exception("Delete failed for %s", sha256)
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"}

    def handle_show(self, sha256: str) -> tuple[int, Dict[str, Any]]:
        # 조회는 공개 가능하므로 인증 없이 메타데이터만 반환한다.
        try:
            asset = self.db.find_asset(sha256)
            if asset is None:
                return HTTPStatus.NOT_FOUND, {"error": "Asset not found"}
            return HTTPStatus.OK, {
                "sha256": asset.sha256,
                "public_url": asset.public_url,
                "status": asset.status,
                "variants": [
                    {
                        "kind": variant.kind,
                        "url": variant.public_url,
                        "width": variant.width,
                        "height": variant.height,
                    }
                    for variant in asset.variants
                ],
            }
        except Exception:
            LOGGER.exception("Asset lookup failed for %s", sha256)
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"}


class UploadRequest:
    """핸들러에서 자주 쓰는 파싱 결과를 한 번에 담아둔다."""

    def __init__(self, handler: BaseHTTPRequestHandler, app: UploadApplication) -> None:
        self.handler = handler
        self.app = app
        self.parsed = urlparse(handler.path)
        self.query = parse_qs(self.parsed.query)


class UploadHandler(BaseHTTPRequestHandler):
    server_version = "image-upload/0.1"

    def do_GET(self) -> None:
        # healthz와 자산 조회는 GET으로 제공한다.
        request = UploadRequest(self, self.server.app)
        if request.parsed.path == "/healthz":
            self.respond(HTTPStatus.OK, {"status": "ok"})
            return
        if request.parsed.path.startswith("/assets/"):
            sha256 = request.parsed.path.rsplit("/", 1)[-1]
            status, payload = self.server.app.handle_show(sha256)
            self.respond(status, payload)
            return
        self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        # 업로드는 인증된 POST 요청만 허용한다.
        request = UploadRequest(self, self.server.app)
        if request.parsed.path == "/upload":
            if not self.server.app.is_authorized(self):
                self.respond(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
                return
            status, payload = self.server.app.handle_upload(request)
            self.respond(status, payload)
            return
        self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_DELETE(self) -> None:
        # 삭제는 인증된 DELETE 요청만 허용한다.
        request = UploadRequest(self, self.server.app)
        if request.parsed.path.startswith("/assets/"):
            if not self.server.app.is_authorized(self):
                self.respond(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
                return
            sha256 = request.parsed.path.rsplit("/", 1)[-1]
            status, payload = self.server.app.handle_delete(sha256)
            self.respond(status, payload)
            return
        self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def log_message(self, format: str, *args) -> None:
        # 기본 HTTP 로그는 끄고 필요 시 구조화 로깅으로 교체한다.
        return

    def respond(self, status: int, payload: Dict[str, Any]) -> None:
        # 모든 응답은 JSON으로 통일해 클라이언트 처리를 단순화한다.
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UploadHTTPServer(ThreadingHTTPServer):
    """핸들러에서 app 객체를 접근할 수 있게 감싼 서버 래퍼."""

    def __init__(self, server_address, handler_cls, app: UploadApplication) -> None:
        super().__init__(server_address, handler_cls)
        self.app = app


def serve() -> None:
    # 설정 로드 -> 준비 작업 -> HTTP 서버 시작 순서로 부팅한다.
    settings = load_settings()
    app = UploadApplication(settings)
    app.ensure_ready()
    server = UploadHTTPServer((settings.host, settings.port), UploadHandler, app)
    print(f"Listening on http://{settings.host}:{settings.port}")
    server.serve_forever()
