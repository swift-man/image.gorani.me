import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from upload_service import deployment_preflight as preflight


class PreflightTests(unittest.TestCase):
    def test_missing_smb_environment_is_silent_config_failure(self):
        # 실제 셸의 환경변수로 누락 경로가 가려지지 않도록 전부 비운다.
        environments = ({}, {"GORANI_SMB_URL": "fixture"},
                        {"GORANI_SMB_MOUNT_POINT": "/fixture"})
        for environment in environments:
            with self.subTest(present=sorted(environment)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(preflight, "load_settings", return_value=Mock()) as settings, \
                        patch.object(preflight, "check_storage") as storage, \
                        patch.object(preflight, "ensure_storage_roots") as roots, \
                        patch.object(preflight, "Database") as database, \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(preflight.main(), 41)
                settings.assert_called_once_with()
                storage.assert_not_called()
                roots.assert_not_called()
                database.assert_not_called()
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_negative_readiness_results(self):
        with patch.dict(os.environ, {"GORANI_SMB_URL": "fixture", "GORANI_SMB_MOUNT_POINT": "/fixture"}), \
                patch.object(preflight, "load_settings", return_value=Mock()), \
                patch.object(preflight, "check_storage", return_value=(False, "private detail")) as storage, \
                patch.object(preflight, "ensure_storage_roots"), \
                patch.object(preflight, "Database") as database:
            self.assertEqual(preflight.main(), 42)
            database.assert_not_called()
            storage.return_value = (True, "ready")
            database.return_value.run_sql.return_value = "not ready"
            self.assertEqual(preflight.main(), 44)

    def test_fixed_codes_and_no_exception_details(self):
        for phase, expected in [(None, 0), ("config", 41), ("smb", 42),
                                ("storage", 43), ("database", 44)]:
            with self.subTest(phase=phase):
                output = io.StringIO()
                secret = RuntimeError("private credential must not escape")
                with patch.dict(os.environ, {"GORANI_SMB_URL": "fixture", "GORANI_SMB_MOUNT_POINT": "/fixture"}), \
                        patch.object(preflight, "load_settings", return_value=Mock(),
                                     side_effect=secret if phase == "config" else None), \
                        patch.object(preflight, "check_storage", return_value=(True, "ready"),
                                     side_effect=secret if phase == "smb" else None), \
                        patch.object(preflight, "ensure_storage_roots",
                                     side_effect=secret if phase == "storage" else None), \
                        patch.object(preflight, "Database") as database, \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    database.return_value.run_sql.return_value = "1"
                    if phase == "database":
                        database.return_value.run_sql.side_effect = secret
                    self.assertEqual(preflight.main(), expected)
                self.assertEqual(output.getvalue(), "")

    def test_ssh_entry_rejects_arbitrary_commands(self):
        entry = Path(__file__).resolve().parents[1] / "scripts/deploy-ssh-entry.sh"
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "Library/Application Support/gorani-image-upload/deploy/sync.sh"
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\nprintf 'fixture-sync'\n")
            target.chmod(0o700)
            installed_entry = target.with_name("ssh-entry.sh")
            installed_entry.write_bytes(entry.read_bytes())
            allowed = 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"'
            for command in ("", "id", allowed + "; id", "scp -t /tmp/file", allowed):
                with self.subTest(command=command):
                    result = subprocess.run(["/bin/sh", str(installed_entry)], capture_output=True, text=True,
                                            env={"HOME": temp, "SSH_ORIGINAL_COMMAND": command}, timeout=5)
                    self.assertEqual(result.returncode, 0 if command == allowed else 64)
                    self.assertEqual(result.stdout, "fixture-sync" if command == allowed else "")

    def test_custom_root_does_not_execute_default_installation(self):
        entry = Path(__file__).resolve().parents[1] / "scripts/deploy-ssh-entry.sh"
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            default = home / "Library/Application Support/gorani-image-upload/deploy"
            custom = home / "custom deployment's directory"
            for root, marker in [(default, "wrong-installation"), (custom, "custom-installation")]:
                root.mkdir(parents=True)
                sync = root / "sync.sh"
                sync.write_text("#!/bin/sh\nprintf '" + marker + "'\n")
                sync.chmod(0o700)
            installed_entry = custom / "ssh-entry.sh"
            installed_entry.write_bytes(entry.read_bytes())
            allowed = 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"'
            result = subprocess.run(["/bin/sh", str(installed_entry)], capture_output=True, text=True,
                                    cwd="/", env={"HOME": temp, "SSH_ORIGINAL_COMMAND": allowed}, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "custom-installation")

    def test_ssh_entry_preserves_failure_without_default_fallback(self):
        entry = Path(__file__).resolve().parents[1] / "scripts/deploy-ssh-entry.sh"
        allowed = 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"'
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            default = home / "Library/Application Support/gorani-image-upload/deploy"
            default.mkdir(parents=True)
            default_sync = default / "sync.sh"
            default_sync.write_text("#!/bin/sh\nprintf 'wrong-installation'\n")
            default_sync.chmod(0o700)
            custom = home / "custom installation"
            custom.mkdir()
            installed_entry = custom / "ssh-entry.sh"
            installed_entry.write_bytes(entry.read_bytes())
            sync = custom / "sync.sh"
            # 없는 실행 파일, 실행 권한 누락, 자식의 실패 종료 모두 성공으로 바꾸면 안 된다.
            for state in ("missing", "not-executable", "failed"):
                with self.subTest(state=state):
                    if state != "missing":
                        sync.write_text("#!/bin/sh\nexit 23\n")
                        sync.chmod(0o700 if state == "failed" else 0o600)
                    result = subprocess.run(["/bin/sh", str(installed_entry)], capture_output=True, text=True,
                                            cwd="/", env={"HOME": temp, "SSH_ORIGINAL_COMMAND": allowed}, timeout=5)
                    if state == "failed":
                        self.assertEqual(result.returncode, 23)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
