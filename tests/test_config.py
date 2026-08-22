from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upload_service import config


class LoadDotenvTests(unittest.TestCase):
    def test_loads_supported_lines_and_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            package_root = project_root / "upload_service"
            package_root.mkdir()
            (project_root / ".env").write_text(
                "\n".join(
                    [
                        "# 로컬 실행 설정",
                        "IMAGE_UPLOAD_PORT=8090",
                        'IMAGE_API_KEYS="quoted-secret"',
                        "MALFORMED_LINE",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, {}, clear=True):
                    config._load_dotenv()

                    self.assertEqual(os.environ["IMAGE_UPLOAD_PORT"], "8090")
                    self.assertEqual(os.environ["IMAGE_API_KEYS"], "quoted-secret")
                    self.assertNotIn("MALFORMED_LINE", os.environ)

    def test_existing_environment_variable_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            package_root = project_root / "upload_service"
            package_root.mkdir()
            (project_root / ".env").write_text("IMAGE_UPLOAD_PORT=8090\n", encoding="utf-8")

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, {"IMAGE_UPLOAD_PORT": "9000"}, clear=True):
                    config._load_dotenv()

                    self.assertEqual(os.environ["IMAGE_UPLOAD_PORT"], "9000")

    def test_missing_dotenv_file_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, {}, clear=True):
                    config._load_dotenv()

                    self.assertEqual(dict(os.environ), {})

    def test_load_settings_uses_dotenv_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            package_root = project_root / "upload_service"
            package_root.mkdir()
            (project_root / ".env").write_text(
                "IMAGE_UPLOAD_PORT=8091\nIMAGE_API_KEYS=first-key,second-key\n",
                encoding="utf-8",
            )

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, {}, clear=True):
                    settings = config.load_settings()

                    self.assertEqual(settings.port, 8091)
                    self.assertEqual(settings.api_keys, frozenset({"first-key", "second-key"}))

    def test_defaults_match_the_shared_storage_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, {"IMAGE_API_KEYS": "test-key"}, clear=True):
                    settings = config.load_settings()

                    self.assertEqual(
                        settings.storage_root,
                        Path("/Volumes/gorani-images/image-store"),
                    )
                    self.assertEqual(settings.thumbnail_format, "webp")
                    self.assertEqual(settings.max_image_pixels, 40_000_000)
                    self.assertEqual(settings.max_workers, 8)
                    self.assertEqual(settings.command_timeout_seconds, 30)
                    self.assertEqual(settings.db_timeout_seconds, 10)
                    self.assertIsNone(settings.database_url)
                    self.assertEqual(settings.pg_database, "postgres")
                    self.assertTrue(settings.require_storage_mount)

    def test_load_settings_rejects_missing_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(ValueError, "IMAGE_API_KEYS"):
                        config.load_settings()

    def test_load_settings_rejects_root_public_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()

            for public_prefix in ("", "/", "///"):
                with self.subTest(public_prefix=public_prefix):
                    environment = {
                        "IMAGE_API_KEYS": "test-key",
                        "IMAGE_PUBLIC_PREFIX": public_prefix,
                    }
                    with patch.object(config, "__file__", str(package_root / "config.py")):
                        with patch.dict(os.environ, environment, clear=True):
                            with self.assertRaisesRegex(ValueError, "IMAGE_PUBLIC_PREFIX"):
                                config.load_settings()

    def test_public_prefix_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()
            environment = {
                "IMAGE_API_KEYS": "test-key",
                "IMAGE_PUBLIC_PREFIX": "images/",
            }

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, environment, clear=True):
                    settings = config.load_settings()

                    self.assertEqual(settings.public_prefix, "/images")

    def test_invalid_boolean_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported boolean value"):
            config._parse_bool("tru", True)

    def test_non_positive_upload_limit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()
            environment = {
                "IMAGE_API_KEYS": "test-key",
                "IMAGE_MAX_UPLOAD_BYTES": "0",
            }

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "IMAGE_MAX_UPLOAD_BYTES"):
                        config.load_settings()

    def test_non_positive_thumbnail_width_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "IMAGE_THUMBNAIL_WIDTHS"):
            config._parse_widths("160,0,320")

    def test_non_positive_worker_limit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()
            environment = {
                "IMAGE_API_KEYS": "test-key",
                "IMAGE_MAX_WORKERS": "0",
            }

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "IMAGE_MAX_WORKERS"):
                        config.load_settings()

    def test_database_url_requires_postgresql_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            package_root = Path(temporary_directory) / "upload_service"
            package_root.mkdir()
            environment = {
                "IMAGE_API_KEYS": "test-key",
                "DATABASE_URL": "mysql://user:password@localhost/images",
            }

            with patch.object(config, "__file__", str(package_root / "config.py")):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
                        config.load_settings()


if __name__ == "__main__":
    unittest.main()
