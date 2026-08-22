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
                with patch.dict(os.environ, {}, clear=True):
                    settings = config.load_settings()

                    self.assertEqual(
                        settings.storage_root,
                        Path("/Volumes/gorani-images/image-store"),
                    )
                    self.assertEqual(settings.thumbnail_format, "webp")
                    self.assertEqual(settings.pg_database, "postgres")


if __name__ == "__main__":
    unittest.main()
