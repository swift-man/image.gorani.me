from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from functools import partial
from http import HTTPStatus
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from upload_service import storage
from upload_service.config import Settings
from upload_service.db import AssetRecord, AssetLookup, Database, VariantRecord
from upload_service.image_ops import ImageInfo
from upload_service.server import MAX_MULTIPART_OVERHEAD_BYTES, UploadApplication
from upload_service.storage import StoredAsset


def make_settings(storage_root: Path, *, require_storage_mount: bool) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8080,
        storage_root=storage_root,
        require_storage_mount=require_storage_mount,
        public_prefix="/i",
        max_upload_bytes=20 * 1024 * 1024,
        enable_thumbnails=True,
        thumbnail_widths=(160, 320, 640),
        thumbnail_format="webp",
        api_keys=frozenset({"test-key"}),
        pg_database="postgres",
    )


class StorageSafetyTests(unittest.TestCase):
    def test_unmounted_storage_is_rejected_before_directories_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            storage_root = Path(temporary_directory) / "missing-share" / "image-store"
            settings = make_settings(storage_root, require_storage_mount=True)

            with patch.object(storage, "_mounted_ancestor", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "not on a mounted volume"):
                    storage.ensure_storage_roots(settings)

            self.assertFalse(storage_root.exists())

    def test_local_storage_can_be_enabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            storage_root = Path(temporary_directory) / "image-store"
            settings = make_settings(storage_root, require_storage_mount=False)

            storage.ensure_storage_roots(settings)

            self.assertTrue(settings.original_root.is_dir())
            self.assertTrue(settings.variants_root.is_dir())
            self.assertEqual(list(storage_root.glob(".write-check-*")), [])

    def test_oversized_upload_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_file_factory = partial(NamedTemporaryFile, dir=temporary_directory)

            with patch.object(storage, "NamedTemporaryFile", temporary_file_factory):
                with self.assertRaisesRegex(ValueError, "Upload exceeds max size"):
                    storage.stage_upload(io.BytesIO(b"123456"), "large.png", max_bytes=5)

            self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    def test_existing_immutable_file_is_reused_without_becoming_rollback_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = root / "stored.png"
            destination.write_bytes(b"existing")
            staged = root / "staged.png"
            staged.write_bytes(b"same-content")

            created = storage.finalize_store(staged, destination)

            self.assertFalse(created)
            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertFalse(staged.exists())

    def test_thumbnail_failure_removes_new_original_and_partial_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            settings = replace(settings, thumbnail_widths=(160,))
            staged = Path(temporary_directory) / "staged.png"
            staged.write_bytes(b"image")
            image_info = ImageInfo("image/png", 800, 600, ".png")
            sha256 = "a" * 64

            def fail_after_writing(_source, destination, _width, _format):
                destination.write_bytes(b"partial")
                raise RuntimeError("thumbnail failed")

            with patch.object(storage, "inspect_image", return_value=image_info):
                with patch.object(storage, "sha256_file", return_value=sha256):
                    with patch.object(storage, "create_thumbnail", side_effect=fail_after_writing):
                        with self.assertRaisesRegex(RuntimeError, "thumbnail failed"):
                            storage.build_asset_record(settings, "photo.png", staged, 5)

            original_path = settings.original_root / "aa" / "aa" / f"{sha256}.png"
            self.assertFalse(original_path.exists())
            self.assertEqual(
                [path for path in settings.variants_root.rglob("*") if path.is_file()],
                [],
            )

    def test_delete_files_is_idempotent_for_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            storage.delete_files([str(Path(temporary_directory) / "already-missing.png")])


class UploadSafetyTests(unittest.TestCase):
    def test_request_size_is_rejected_before_multipart_body_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            application = UploadApplication(settings)
            body = Mock()
            request = SimpleNamespace(
                handler=SimpleNamespace(
                    headers={
                        "Content-Type": "multipart/form-data; boundary=test",
                        "Content-Length": str(
                            settings.max_upload_bytes + MAX_MULTIPART_OVERHEAD_BYTES + 1
                        ),
                    },
                    rfile=body,
                )
            )

            status, payload = application.handle_upload(request)

            self.assertEqual(status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            self.assertIn("exceeds max size", payload["error"])
            body.read.assert_not_called()

    def test_database_failure_does_not_delete_reused_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            application = UploadApplication(settings)
            existing_path = root / "existing.png"
            existing_path.write_bytes(b"existing")
            asset = AssetRecord(
                sha256="b" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=5,
                width=10,
                height=10,
                storage_path=str(existing_path),
                public_url="/i/original/bb/bb/photo.png",
            )
            stored = StoredAsset(asset=asset, variants=[], created_paths=[])
            request = _multipart_request(b"image")

            with patch("upload_service.server.build_asset_record", return_value=stored):
                with patch.object(application.db, "insert_asset", side_effect=RuntimeError("db failed")):
                    with patch("upload_service.server.LOGGER.exception"):
                        status, payload = application.handle_upload(request)

            self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertEqual(payload, {"error": "Internal server error"})
            self.assertEqual(existing_path.read_bytes(), b"existing")

    def test_database_failure_removes_files_created_by_the_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            application = UploadApplication(settings)
            created_path = root / "created.png"
            created_path.write_bytes(b"created")
            asset = AssetRecord(
                sha256="e" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=5,
                width=10,
                height=10,
                storage_path=str(created_path),
                public_url="/i/original/ee/ee/photo.png",
            )
            stored = StoredAsset(asset=asset, variants=[], created_paths=[str(created_path)])
            request = _multipart_request(b"image")

            with patch("upload_service.server.build_asset_record", return_value=stored):
                with patch.object(application.db, "insert_asset", side_effect=RuntimeError("db failed")):
                    with patch("upload_service.server.LOGGER.exception"):
                        status, _ = application.handle_upload(request)

            self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertFalse(created_path.exists())

    def test_failed_delete_can_be_retried_after_files_are_already_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            application = UploadApplication(settings)
            original_path = root / "original.png"
            variant_path = root / "variant.webp"
            original_path.write_bytes(b"original")
            variant_path.write_bytes(b"variant")
            asset = AssetLookup(
                asset_id=1,
                sha256="c" * 64,
                storage_path=str(original_path),
                public_url="/i/original/cc/cc/original.png",
                status="active",
                variants=[
                    VariantRecord(
                        kind="thumb_160",
                        format="webp",
                        width=160,
                        height=120,
                        byte_size=7,
                        storage_path=str(variant_path),
                        public_url="/i/variants/cc/cc/variant.webp",
                    )
                ],
            )
            application.db.find_asset = Mock(return_value=asset)
            application.db.mark_deleted = Mock(side_effect=[RuntimeError("db failed"), None])

            with patch("upload_service.server.LOGGER.exception"):
                first_status, _ = application.handle_delete(asset.sha256)
                second_status, second_payload = application.handle_delete(asset.sha256)

            self.assertEqual(first_status, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertEqual(second_status, HTTPStatus.OK)
            self.assertEqual(second_payload["status"], "deleted")
            self.assertFalse(original_path.exists())
            self.assertFalse(variant_path.exists())


class DatabaseSafetyTests(unittest.TestCase):
    def test_asset_and_variants_are_written_in_one_database_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            database = Database(settings)
            asset = AssetRecord(
                sha256="d" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=100,
                width=800,
                height=600,
                storage_path="/store/original.png",
                public_url="/i/original/dd/dd/original.png",
            )
            variant = VariantRecord(
                kind="thumb_160",
                format="webp",
                width=160,
                height=120,
                byte_size=20,
                storage_path="/store/variant.webp",
                public_url="/i/variants/dd/dd/variant.webp",
            )

            with patch.object(database, "run_sql", return_value="42") as run_sql:
                asset_id = database.insert_asset(asset, [variant])

            self.assertEqual(asset_id, 42)
            run_sql.assert_called_once()
            sql = run_sql.call_args.args[0]
            self.assertIn("upserted_variants", sql)
            self.assertIn("CROSS JOIN variant_input", sql)


def _multipart_request(content: bytes):
    boundary = "gorani-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="photo.png"\r\n'
        "Content-Type: image/png\r\n"
        "\r\n"
    ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    return SimpleNamespace(
        handler=SimpleNamespace(
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            rfile=io.BytesIO(body),
        )
    )


class AuthorizationSafetyTests(unittest.TestCase):
    def test_empty_api_key_set_denies_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            settings = replace(settings, api_keys=frozenset())
            application = UploadApplication(settings)
            handler = SimpleNamespace(headers={})

            self.assertFalse(application.is_authorized(handler))


if __name__ == "__main__":
    unittest.main()
