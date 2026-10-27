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
            allowed = 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"'
            for command in ("", "id", allowed + "; id", "scp -t /tmp/file", allowed):
                with self.subTest(command=command):
                    result = subprocess.run(["/bin/sh", str(entry)], capture_output=True, text=True,
                                            env={"HOME": temp, "SSH_ORIGINAL_COMMAND": command}, timeout=5)
                    self.assertEqual(result.returncode, 0 if command == allowed else 64)
                    self.assertEqual(result.stdout, "fixture-sync" if command == allowed else "")
