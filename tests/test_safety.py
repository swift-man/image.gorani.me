from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from unittest.mock import patch

from upload_service import storage
from upload_service.config import Settings
from upload_service.server import UploadApplication


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
