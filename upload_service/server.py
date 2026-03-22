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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings)

    def ensure_ready(self) -> None:
        ensure_storage_roots(self.settings)
        schema_path = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
        self.db.apply_schema(schema_path)

    def is_authorized(self, handler: BaseHTTPRequestHandler) -> bool:
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
            temp_path, byte_size = stage_upload(file_item.file, original_filename, self.settings.max_upload_bytes)
            stored = build_asset_record(self.settings, original_filename, temp_path, byte_size)
            asset_id = self.db.insert_asset(stored.asset, stored.variants)
        except ValueError as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if stored is not None:
                delete_files([variant.storage_path for variant in stored.variants] + [stored.asset.storage_path])
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if stored is not None:
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
        asset = self.db.find_asset(sha256)
        if asset is None:
            return HTTPStatus.NOT_FOUND, {"error": "Asset not found"}

        delete_files([variant.storage_path for variant in asset.variants] + [asset.storage_path])
        self.db.mark_deleted(sha256)
        return HTTPStatus.OK, {"status": "deleted", "sha256": sha256}

    def handle_show(self, sha256: str) -> tuple[int, Dict[str, Any]]:
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
    def __init__(self, handler: BaseHTTPRequestHandler, app: UploadApplication) -> None:
        self.handler = handler
        self.app = app
        self.parsed = urlparse(handler.path)
        self.query = parse_qs(self.parsed.query)


class UploadHandler(BaseHTTPRequestHandler):
    server_version = "image-upload/0.1"

    def do_GET(self) -> None:
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
        return

    def respond(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UploadHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_cls, app: UploadApplication) -> None:
        super().__init__(server_address, handler_cls)
        self.app = app


def serve() -> None:
    settings = load_settings()
    app = UploadApplication(settings)
    app.ensure_ready()
    server = UploadHTTPServer((settings.host, settings.port), UploadHandler, app)
    print(f"Listening on http://{settings.host}:{settings.port}")
    server.serve_forever()
