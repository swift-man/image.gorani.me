from __future__ import annotations

import io
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from functools import partial
from http import HTTPStatus
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from python_multipart.exceptions import MultipartParseError

from upload_service import __version__, storage
from upload_service import image_ops
from upload_service.config import Settings
from upload_service.db import (
    AssetLookup,
    AssetRecord,
    Database,
    PendingFileDeletion,
    VariantRecord,
    _sql_literal,
)
from upload_service.errors import (
    DatabaseTimeoutError,
    ImageProcessingTimeoutError,
    InvalidImageError,
)
from upload_service.fix_storage_root import update_storage_root
from upload_service.image_ops import ImageInfo
from upload_service.server import (
    MAX_MULTIPART_OVERHEAD_BYTES,
    UploadApplication,
    UploadHandler,
    UploadHTTPServer,
)
from upload_service.storage import StoredAsset


class VersionContractTests(unittest.TestCase):
    def test_http_server_header_uses_the_release_version(self) -> None:
        self.assertEqual(UploadHandler.server_version, f"image-upload/{__version__}")


def make_settings(storage_root: Path, *, require_storage_mount: bool) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8080,
        storage_root=storage_root,
        require_storage_mount=require_storage_mount,
        public_prefix="/i",
        max_upload_bytes=20 * 1024 * 1024,
        max_image_pixels=40_000_000,
        max_workers=8,
        http_timeout_seconds=30,
        cleanup_batch_size=10,
        command_timeout_seconds=30,
        db_timeout_seconds=10,
        enable_thumbnails=True,
        thumbnail_widths=(160, 320, 640),
        thumbnail_format="webp",
        api_keys=frozenset({"test-key"}),
        database_url=None,
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

            def fail_after_writing(_source, destination, _width, _format, _timeout, _pixels):
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

    def test_pixel_limit_is_checked_before_final_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            settings = replace(settings, max_image_pixels=100)
            staged = Path(temporary_directory) / "staged.png"
            staged.write_bytes(b"image")
            image_info = ImageInfo("image/png", 11, 10, ".png")

            with patch.object(storage, "inspect_image", return_value=image_info):
                with patch.object(storage, "finalize_store") as finalize_store:
                    with self.assertRaisesRegex(ValueError, "max pixel count"):
                        storage.build_asset_record(settings, "photo.png", staged, 5)

            finalize_store.assert_not_called()

    @unittest.skipUnless(shutil.which("sips"), "macOS sips is required")
    def test_exif_formats_drive_metadata_and_prevent_thumbnail_upscaling(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root / "store", require_storage_mount=False)
            settings = replace(settings, thumbnail_widths=(10, 30))
            supported_formats = (
                ("JPEG", ".jpg"),
                ("PNG", ".png"),
                ("WEBP", ".webp"),
            )
            for image_format, suffix in supported_formats:
                with self.subTest(image_format=image_format):
                    staged = root / f"orientation-6{suffix}"
                    image = Image.new("RGB", (40, 20), "red")
                    exif = Image.Exif()
                    exif[274] = 6
                    image.save(staged, format=image_format, exif=exif)
                    image.close()

                    stored = storage.build_asset_record(
                        settings,
                        staged.name,
                        staged,
                        staged.stat().st_size,
                    )

                    self.assertEqual(
                        (stored.asset.width, stored.asset.height),
                        (20, 40),
                    )
                    self.assertEqual(len(stored.variants), 1)
                    self.assertEqual(stored.variants[0].kind, "thumb_10")
                    self.assertEqual(
                        (stored.variants[0].width, stored.variants[0].height),
                        (10, 20),
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
                    with patch.object(application.db, "find_asset", return_value=None):
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
                    with patch.object(application.db, "find_asset", return_value=None):
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
                original_filename="original.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=8,
                width=10,
                height=10,
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
            application.db.mark_deleting = Mock()
            application.db.mark_deleted = Mock(side_effect=[RuntimeError("db failed"), None])
            application.db.find_pending_file_deletions = Mock(return_value=[])

            with patch("upload_service.server.LOGGER.exception"):
                first_status, _ = application.handle_delete(asset.sha256)
                second_status, second_payload = application.handle_delete(asset.sha256)

            self.assertEqual(first_status, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertEqual(second_status, HTTPStatus.OK)
            self.assertEqual(second_payload["status"], "deleted")
            self.assertFalse(original_path.exists())
            self.assertFalse(variant_path.exists())
            self.assertEqual(application.db.mark_deleting.call_count, 2)

    def test_image_field_over_limit_returns_413(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            settings = replace(settings, max_upload_bytes=5)
            application = UploadApplication(settings)

            status, payload = application.handle_upload(_multipart_request(b"123456"))

            self.assertEqual(status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            self.assertIn("exceeds max size", payload["error"])

    def test_ambiguous_database_success_preserves_files_and_returns_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            application = UploadApplication(settings)
            created_path = root / "created.png"
            created_path.write_bytes(b"created")
            asset = AssetRecord(
                sha256="9" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=5,
                width=10,
                height=10,
                storage_path=str(created_path),
                public_url="/i/original/99/99/photo.png",
            )
            stored = StoredAsset(asset=asset, variants=[], created_paths=[str(created_path)])
            persisted = AssetLookup(
                asset_id=99,
                sha256=asset.sha256,
                original_filename=asset.original_filename,
                content_type=asset.content_type,
                file_ext=asset.file_ext,
                byte_size=asset.byte_size,
                width=asset.width,
                height=asset.height,
                storage_path=asset.storage_path,
                public_url=asset.public_url,
                status="active",
                variants=[],
            )

            with patch("upload_service.server.sha256_file", return_value=asset.sha256):
                with patch("upload_service.server.build_asset_record", return_value=stored):
                    with patch.object(
                        application.db,
                        "insert_asset",
                        side_effect=RuntimeError("lost reply"),
                    ):
                        with patch.object(application.db, "find_asset", return_value=persisted):
                            with patch.object(
                                application.db,
                                "find_pending_file_deletions",
                                return_value=[],
                            ):
                                with patch("upload_service.server.LOGGER.warning"):
                                    status, payload = application.handle_upload(
                                        _multipart_request(b"image")
                                    )

            self.assertEqual(status, HTTPStatus.CREATED)
            self.assertEqual(payload["id"], 99)
            self.assertTrue(created_path.exists())

    def test_ambiguous_database_result_requires_exact_variant_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            application = UploadApplication(settings)
            asset = AssetRecord(
                sha256="8" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=5,
                width=10,
                height=10,
                storage_path="/store/original.png",
                public_url="/i/original/88/88/original.png",
            )
            stale_variant = VariantRecord(
                kind="thumb_160",
                format="webp",
                width=160,
                height=120,
                byte_size=20,
                storage_path="/store/stale.webp",
                public_url="/i/variants/88/88/stale.webp",
            )
            stored = StoredAsset(asset=asset, variants=[], created_paths=[])
            persisted = AssetLookup(
                asset_id=88,
                sha256=asset.sha256,
                original_filename=asset.original_filename,
                content_type=asset.content_type,
                file_ext=asset.file_ext,
                byte_size=asset.byte_size,
                width=asset.width,
                height=asset.height,
                storage_path=asset.storage_path,
                public_url=asset.public_url,
                status="active",
                variants=[stale_variant],
            )

            self.assertFalse(application._asset_matches_stored(persisted, stored))

    def test_ambiguous_database_result_rejects_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            application = UploadApplication(settings)
            asset = AssetRecord(
                sha256="7" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=100,
                width=800,
                height=600,
                storage_path="/store/original.png",
                public_url="/i/original/77/77/original.png",
            )
            expected_variant = VariantRecord(
                kind="thumb_160",
                format="webp",
                width=160,
                height=120,
                byte_size=20,
                storage_path="/store/thumb_160.webp",
                public_url="/i/variants/77/77/thumb_160.webp",
            )
            stored = StoredAsset(
                asset=asset,
                variants=[expected_variant],
                created_paths=[],
            )
            persisted = AssetLookup(
                asset_id=77,
                sha256=asset.sha256,
                original_filename=asset.original_filename,
                content_type=asset.content_type,
                file_ext=asset.file_ext,
                byte_size=asset.byte_size,
                width=asset.width,
                height=asset.height,
                storage_path=asset.storage_path,
                public_url="/i/old/original.png",
                status="active",
                variants=[expected_variant],
            )

            self.assertFalse(application._asset_matches_stored(persisted, stored))

            persisted.public_url = asset.public_url
            persisted.variants = [replace(expected_variant, width=159)]
            self.assertFalse(application._asset_matches_stored(persisted, stored))

    def test_multipart_parse_error_is_reported_as_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            application = UploadApplication(settings)

            with patch(
                "upload_service.server.parse_form",
                side_effect=MultipartParseError("broken boundary"),
            ):
                status, payload = application.handle_upload(_multipart_request(b"broken"))

            self.assertEqual(status, HTTPStatus.BAD_REQUEST)
            self.assertEqual(payload, {"error": "Malformed multipart body"})

    def test_unverifiable_database_result_preserves_files_for_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            application = UploadApplication(settings)
            created_path = root / "created.png"
            created_path.write_bytes(b"created")
            asset = AssetRecord(
                sha256="6" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=5,
                width=10,
                height=10,
                storage_path=str(created_path),
                public_url="/i/original/66/66/photo.png",
            )
            stored = StoredAsset(asset=asset, variants=[], created_paths=[str(created_path)])
            timeout = DatabaseTimeoutError("database unavailable")

            with patch("upload_service.server.sha256_file", return_value=asset.sha256):
                with patch("upload_service.server.build_asset_record", return_value=stored):
                    with patch.object(application.db, "insert_asset", side_effect=timeout):
                        with patch.object(application.db, "find_asset", side_effect=timeout):
                            with patch("upload_service.server.LOGGER.exception"):
                                status, payload = application.handle_upload(
                                    _multipart_request(b"image")
                                )

            self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
            self.assertEqual(payload, {"error": "Service temporarily unavailable"})
            self.assertTrue(created_path.exists())

    def test_image_command_timeout_returns_503(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            application = UploadApplication(settings)

            with patch(
                "upload_service.server.build_asset_record",
                side_effect=ImageProcessingTimeoutError("sips timed out"),
            ):
                with patch("upload_service.server.LOGGER.exception"):
                    status, payload = application.handle_upload(_multipart_request(b"image"))

            self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
            self.assertEqual(payload, {"error": "Service temporarily unavailable"})

    def test_invalid_decoder_input_returns_400_and_removes_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            application = UploadApplication(settings)
            staged = Path(temporary_directory) / "invalid.upload"
            staged.write_bytes(b"not-an-image")

            with patch(
                "upload_service.server.stage_upload",
                return_value=(staged, staged.stat().st_size),
            ):
                with patch.object(
                    application,
                    "_persist_staged_upload",
                    side_effect=InvalidImageError("Unable to decode image"),
                ):
                    status, payload = application.handle_upload(
                        _multipart_request(b"not-an-image")
                    )

            self.assertEqual(status, HTTPStatus.BAD_REQUEST)
            self.assertEqual(payload, {"error": "Unable to decode image"})
            self.assertFalse(staged.exists())

    def test_pending_variant_file_cleanup_is_idempotently_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            application = UploadApplication(settings)
            retired_path = root / "retired.jpg"
            retired_path.write_bytes(b"retired")
            pending = PendingFileDeletion(7, str(retired_path), "7" * 64)
            application.db.find_pending_file_deletions = Mock(return_value=[pending])
            application.db.is_file_deletion_ready = Mock(return_value=True)
            application.db.mark_file_deletion_completed = Mock(
                side_effect=[RuntimeError("db failed"), None]
            )

            with patch("upload_service.server.LOGGER.exception"):
                application._cleanup_pending_files()
                application._cleanup_pending_files()

            self.assertFalse(retired_path.exists())
            self.assertEqual(application.db.mark_file_deletion_completed.call_count, 2)

    def test_pending_cleanup_rechecks_active_references_after_locking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            settings = replace(settings, cleanup_batch_size=1)
            application = UploadApplication(settings)
            current_path = root / "current.webp"
            current_path.write_bytes(b"current")
            pending = PendingFileDeletion(8, str(current_path), "8" * 64)
            application.db.find_pending_file_deletions = Mock(return_value=[pending])
            application.db.is_file_deletion_ready = Mock(return_value=False)
            application.db.mark_file_deletion_completed = Mock()

            should_continue = application._cleanup_pending_files()

            self.assertTrue(current_path.exists())
            self.assertFalse(should_continue)
            application.db.mark_file_deletion_completed.assert_not_called()

    def test_pending_cleanup_uses_a_fixed_batch_and_stops_on_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = make_settings(root, require_storage_mount=False)
            settings = replace(settings, cleanup_batch_size=2)
            application = UploadApplication(settings)
            pending = [
                PendingFileDeletion(1, str(root / "first.webp"), "1" * 64),
                PendingFileDeletion(2, str(root / "second.webp"), "2" * 64),
            ]
            application.db.find_pending_file_deletions = Mock(return_value=pending)
            application.db.is_file_deletion_ready = Mock(
                side_effect=DatabaseTimeoutError("database timed out")
            )
            application.db.mark_file_deletion_completed = Mock()

            with patch("upload_service.server.LOGGER.exception"):
                should_continue = application._cleanup_pending_files()

            self.assertFalse(should_continue)
            application.db.find_pending_file_deletions.assert_called_once_with(2)
            self.assertEqual(application.db.is_file_deletion_ready.call_count, 1)
            application.db.mark_file_deletion_completed.assert_not_called()

    def test_cleanup_is_scheduled_without_running_on_the_request_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            application = UploadApplication(settings)
            application._cleanup_thread = Mock()

            with patch.object(application, "_cleanup_pending_files") as cleanup:
                application._schedule_cleanup()

            cleanup.assert_not_called()
            self.assertTrue(application._cleanup_event.is_set())


class DatabaseSafetyTests(unittest.TestCase):
    def test_sql_literal_encodes_untrusted_text_as_hex(self) -> None:
        literal = _sql_literal("photo'); DROP TABLE assets;--.png")

        self.assertNotIn("DROP TABLE", literal)
        self.assertRegex(literal, r"^convert_from\(decode\('[0-9a-f]+'")

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
            self.assertIn("pending_file_deletions", sql)
            self.assertIn("queued_removed_variant_files", sql)
            self.assertIn("retired_variants", sql)
            self.assertIn("'variant_removed'", sql)

    def test_empty_variant_input_retires_all_active_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            database = Database(settings)
            asset = AssetRecord(
                sha256="e" * 64,
                original_filename="photo.png",
                content_type="image/png",
                file_ext=".png",
                byte_size=100,
                width=800,
                height=600,
                storage_path="/store/original.png",
                public_url="/i/original/ee/ee/original.png",
            )

            with patch.object(database, "run_sql", return_value="42") as run_sql:
                asset_id = database.insert_asset(asset, [])

            self.assertEqual(asset_id, 42)
            sql = run_sql.call_args.args[0]
            self.assertIn("WHERE FALSE", sql)
            self.assertIn("queued_removed_variant_files", sql)
            self.assertIn("retired_variants", sql)
            self.assertIn("completed_at = NULL", sql)

    def test_delete_starts_with_a_recoverable_database_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            database = Database(settings)

            with patch.object(database, "run_sql") as run_sql:
                database.mark_deleting("f" * 64)

            sql = run_sql.call_args.args[0]
            self.assertIn("status = 'deleting'", sql)
            self.assertIn("status IN ('active', 'deleting')", sql)

    def test_database_url_secret_is_not_added_to_process_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            password = "unit-test-password"
            database_url = "".join(
                [
                    "postgresql",
                    "://",
                    "image-user",
                    ":",
                    password,
                    "@",
                    "db.local:5433/images?sslmode=require",
                ]
            )
            settings = replace(
                settings,
                database_url=database_url,
            )
            database = Database(settings)

            command = database._psql_base_command()
            environment = database._psql_environment()

            self.assertNotIn(password, " ".join(command))
            self.assertEqual(environment["PGPASSWORD"], password)
            self.assertEqual(environment["PGDATABASE"], "images")
            self.assertEqual(environment["PGSSLMODE"], "require")

    def test_database_command_timeout_is_converted_to_service_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            database = Database(settings)

            with patch(
                "upload_service.db.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["psql"], 1),
            ):
                with self.assertRaises(DatabaseTimeoutError):
                    database.run_sql("SELECT 1")

    def test_postgresql_statement_timeout_is_converted_to_service_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            database = Database(settings)
            timeout_error = subprocess.CalledProcessError(
                1,
                ["psql"],
                stderr="ERROR: canceling statement due to statement timeout",
            )

            with patch("upload_service.db.subprocess.run", side_effect=timeout_error):
                with self.assertRaises(DatabaseTimeoutError):
                    database.run_sql("SELECT pg_sleep(30)")

    def test_storage_root_update_uses_one_checked_transaction(self) -> None:
        database = Mock()

        update_storage_root(
            database,
            "/tmp/ksj's_%_root",
            "/Volumes/gorani-images/image-store",
        )

        database.run_sql.assert_called_once()
        sql = database.run_sql.call_args.args[0]
        self.assertIn("BEGIN;", sql)
        self.assertIn("to_regclass('assets')", sql)
        self.assertIn("to_regclass('asset_variants')", sql)
        self.assertIn("to_regclass('pending_file_deletions')", sql)
        self.assertIn("UPDATE pending_file_deletions", sql)
        self.assertIn("WHERE completed_at IS NULL", sql)
        self.assertIn("COMMIT;", sql)
        self.assertNotIn("ksj's_%_root", sql)


class ImageCommandSafetyTests(unittest.TestCase):
    def test_image_command_timeout_is_converted_to_service_timeout(self) -> None:
        timeout = subprocess.TimeoutExpired(["file"], 1)
        with patch("upload_service.image_ops.subprocess.run", side_effect=timeout):
            with self.assertRaises(ImageProcessingTimeoutError):
                image_ops.detect_content_type(Path("image.png"), timeout_seconds=1)

    def test_download_filename_is_reduced_to_a_bounded_basename(self) -> None:
        filename = "../private/" + ("a" * 500) + ".png"

        result = image_ops.guess_download_name(filename, ".png")

        self.assertNotIn("/", result)
        self.assertLessEqual(len(result), 200)
        self.assertTrue(result.endswith(".png"))

    def test_sips_decode_failure_is_classified_as_invalid_image(self) -> None:
        command_error = subprocess.CalledProcessError(1, ["sips"], stderr="invalid image")

        with patch.object(image_ops, "detect_content_type", return_value="image/jpeg"):
            with patch.object(image_ops, "_run_image_command", side_effect=command_error):
                with self.assertRaises(InvalidImageError):
                    image_ops.inspect_image(Path("broken.jpg"))

    def test_file_identification_failure_is_classified_as_invalid_image(self) -> None:
        command_error = subprocess.CalledProcessError(1, ["file"], stderr="invalid image")

        with patch.object(image_ops, "_run_image_command", side_effect=command_error):
            with self.assertRaises(InvalidImageError):
                image_ops.inspect_image(Path("broken.jpg"))

    def test_pillow_thumbnail_timeout_is_enforced_by_worker_process(self) -> None:
        timeout = subprocess.TimeoutExpired(
            ["python", "-m", "upload_service.pillow_worker"],
            1,
        )

        with patch("upload_service.image_ops.subprocess.run", side_effect=timeout):
            with self.assertRaises(ImageProcessingTimeoutError):
                image_ops.create_thumbnail_with_pillow(
                    Path("source.jpg"),
                    Path("thumb.webp"),
                    width=160,
                    output_format="webp",
                    timeout_seconds=1,
                )

    def test_pillow_thumbnail_applies_exif_orientation(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "oriented.jpg"
            destination = Path(temporary_directory) / "thumb.webp"
            image = Image.new("RGB", (40, 20), "red")
            exif = Image.Exif()
            exif[274] = 6
            image.save(source, format="JPEG", exif=exif)
            image.close()

            image_ops.render_thumbnail_with_pillow(
                source,
                destination,
                width=10,
                output_format="webp",
                max_pixels=1_000_000,
            )

            with Image.open(destination) as thumbnail:
                self.assertEqual(thumbnail.size, (10, 20))

    def test_exif_orientations_normalize_metadata_dimensions(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary_directory:
            for orientation in (6, 8):
                with self.subTest(orientation=orientation):
                    source = Path(temporary_directory) / f"orientation-{orientation}.jpg"
                    image = Image.new("RGB", (40, 20), "red")
                    exif = Image.Exif()
                    exif[274] = orientation
                    image.save(source, format="JPEG", exif=exif)
                    image.close()

                    dimensions = image_ops.read_oriented_dimensions_with_pillow(
                        source,
                        max_pixels=1_000_000,
                    )

                    self.assertEqual(dimensions, (20, 40))

    @unittest.skipUnless(shutil.which("sips"), "macOS sips is required")
    def test_inspect_image_returns_display_oriented_dimensions(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "orientation-6.jpg"
            image = Image.new("RGB", (40, 20), "red")
            exif = Image.Exif()
            exif[274] = 6
            image.save(source, format="JPEG", exif=exif)
            image.close()

            info = image_ops.inspect_image(source, timeout_seconds=10)

            self.assertEqual((info.width, info.height), (20, 40))

    def test_pillow_uses_rgb_for_opaque_images_and_preserves_alpha(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            opaque_source = root / "opaque.jpg"
            opaque_output = root / "opaque.webp"
            alpha_source = root / "alpha.png"
            alpha_output = root / "alpha.webp"
            Image.new("RGB", (20, 20), "red").save(opaque_source, "JPEG")
            Image.new("RGBA", (20, 20), (255, 0, 0, 64)).save(alpha_source, "PNG")

            image_ops.render_thumbnail_with_pillow(
                opaque_source,
                opaque_output,
                width=10,
                output_format="webp",
                max_pixels=1_000_000,
            )
            image_ops.render_thumbnail_with_pillow(
                alpha_source,
                alpha_output,
                width=10,
                output_format="webp",
                max_pixels=1_000_000,
            )

            with Image.open(opaque_output) as opaque_thumbnail:
                self.assertEqual(opaque_thumbnail.mode, "RGB")
            with Image.open(alpha_output) as alpha_thumbnail:
                self.assertEqual(alpha_thumbnail.mode, "RGBA")
                self.assertLess(alpha_thumbnail.getpixel((0, 0))[3], 255)

    @unittest.skipUnless(shutil.which("sips"), "macOS sips is required")
    def test_pillow_worker_process_creates_an_oriented_thumbnail(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "oriented.jpg"
            destination = Path(temporary_directory) / "thumb.webp"
            image = Image.new("RGB", (40, 20), "red")
            exif = Image.Exif()
            exif[274] = 6
            image.save(source, format="JPEG", exif=exif)
            image.close()

            info = image_ops.create_thumbnail_with_pillow(
                source,
                destination,
                width=10,
                output_format="webp",
                timeout_seconds=10,
                max_pixels=1_000_000,
            )

            self.assertEqual((info.width, info.height), (10, 20))
            self.assertTrue(destination.is_file())

    def test_pillow_rejects_unsupported_decoder_input_as_invalid_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "unsupported.heic"
            destination = Path(temporary_directory) / "thumb.webp"
            source.write_bytes(b"unsupported-image-data")

            with self.assertRaises(InvalidImageError):
                image_ops.render_thumbnail_with_pillow(
                    source,
                    destination,
                    width=10,
                    output_format="webp",
                    max_pixels=1_000_000,
                )


class ServerConcurrencySafetyTests(unittest.TestCase):
    def test_http_server_has_bounded_worker_and_socket_queues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            application = UploadApplication(settings)
            server = UploadHTTPServer(
                ("127.0.0.1", 0),
                UploadHandler,
                application,
                max_workers=2,
                http_timeout_seconds=30,
            )
            try:
                self.assertEqual(server.request_queue_size, 2)
                self.assertTrue(server._worker_slots.acquire(blocking=False))
                self.assertTrue(server._worker_slots.acquire(blocking=False))
                self.assertFalse(server._worker_slots.acquire(blocking=False))
            finally:
                server.server_close()

    def test_partial_request_body_times_out_and_releases_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = make_settings(Path(temporary_directory), require_storage_mount=False)
            settings = replace(settings, max_workers=1, http_timeout_seconds=1)
            application = UploadApplication(settings)
            server = UploadHTTPServer(
                ("127.0.0.1", 0),
                UploadHandler,
                application,
                max_workers=1,
                http_timeout_seconds=1,
            )
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            host, port = server.server_address
            try:
                with socket.create_connection((host, port), timeout=3) as client:
                    client.settimeout(4)
                    client.sendall(
                        b"POST /upload HTTP/1.1\r\n"
                        b"Host: localhost\r\n"
                        b"X-API-Key: test-key\r\n"
                        b"Content-Type: multipart/form-data; boundary=slow\r\n"
                        b"Content-Length: 1000\r\n\r\n"
                        b"--slow\r\n"
                    )
                    response = client.recv(4096)

                self.assertIn(b"408 Request Timeout", response)

                with socket.create_connection((host, port), timeout=3) as health_client:
                    health_client.settimeout(3)
                    health_client.sendall(
                        b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n"
                    )
                    health_response = health_client.recv(4096)

                self.assertIn(b"200 OK", health_response)
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=3)


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
