from __future__ import annotations

import cgi
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .config import Settings, load_settings
from .db import Database
from .storage import build_asset_record, delete_files, ensure_storage_roots, stage_upload


class UploadApplication:
    """업로드 서비스의 핵심 유스케이스를 묶는 애플리케이션 계층."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings)

    def ensure_ready(self) -> None:
        # 서버 시작 시 저장소 루트와 DB 스키마를 준비한다.
        ensure_storage_roots(self.settings)
        schema_path = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
        self.db.apply_schema(schema_path)

    def is_authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        # 읽기 API는 공개하고, 쓰기 API만 키 기반으로 보호한다.
        if not self.settings.api_keys:
            return True

        x_api_key = handler.headers.get("X-API-Key", "").strip()
        if x_api_key and x_api_key in self.settings.api_keys:
            return True

        auth_header = handler.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token in self.settings.api_keys:
                return True

        return False

    def handle_upload(self, request: "UploadRequest") -> tuple[int, Dict[str, Any]]:
        # 업로드는 multipart/form-data만 허용한다.
        content_type = request.handler.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return HTTPStatus.BAD_REQUEST, {"error": "Content-Type must be multipart/form-data"}

        form = cgi.FieldStorage(
            fp=request.handler.rfile,
            headers=request.handler.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
            },
        )
        file_item = form["image"] if "image" in form else None
        if file_item is None or not getattr(file_item, "file", None):
            return HTTPStatus.BAD_REQUEST, {"error": "Missing form field: image"}

        original_filename = file_item.filename or "upload"
        temp_path = None
        stored = None
        try:
            # 먼저 임시 파일로 받은 뒤, 검사와 저장을 진행한다.
            temp_path, byte_size = stage_upload(file_item.file, original_filename, self.settings.max_upload_bytes)
            stored = build_asset_record(self.settings, original_filename, temp_path, byte_size)
            asset_id = self.db.insert_asset(stored.asset, stored.variants)
        except ValueError as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if stored is not None:
                # 저장 중간에 실패하면 원본/썸네일을 최대한 롤백한다.
                delete_files([variant.storage_path for variant in stored.variants] + [stored.asset.storage_path])
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if stored is not None:
                # 예기치 못한 예외도 파일은 남기지 않도록 정리한다.
                delete_files([variant.storage_path for variant in stored.variants] + [stored.asset.storage_path])
            raise

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
        # 삭제는 파일 제거 후 DB 상태를 deleted로 전환한다.
        asset = self.db.find_asset(sha256)
        if asset is None:
            return HTTPStatus.NOT_FOUND, {"error": "Asset not found"}

        delete_files([variant.storage_path for variant in asset.variants] + [asset.storage_path])
        self.db.mark_deleted(sha256)
        return HTTPStatus.OK, {"status": "deleted", "sha256": sha256}

    def handle_show(self, sha256: str) -> tuple[int, Dict[str, Any]]:
        # 조회는 공개 가능하므로 인증 없이 메타데이터만 반환한다.
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
