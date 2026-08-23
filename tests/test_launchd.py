from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upload_service.macos_runtime import (
    check_storage,
    mount_matches,
    render_launch_agent,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LABEL = "me.gorani.image-upload"


class LaunchAgentAssetTests(unittest.TestCase):
    def test_launch_agent_template_has_expected_runtime_contract(self) -> None:
        template_path = PROJECT_ROOT / "launchd" / f"{LABEL}.plist.template"
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "agent.plist"
            render_launch_agent(
                template_path,
                output_path,
                {
                    "app_executable": "/tmp/home/Applications/A&B.app/Contents/MacOS/App",
                    "supervisor_script": "/tmp/image|gorani/scripts/supervise.sh",
                    "project_root": "/tmp/image<gorani>",
                    "runtime_path": "/opt/homebrew/bin:/usr/bin:/bin",
                    "smb_host": "DESKTOP-0217PLD",
                    "smb_url": "smb://ksj@DESKTOP-0217PLD/gorani&images",
                    "mount_point": "/Volumes/gorani<images>",
                    "storage_root": "/Volumes/gorani<images>/image-store",
                    "upload_host": "127.0.0.1",
                    "upload_port": "8090",
                    "stdout_path": "/tmp/logs/service&out.log",
                    "stderr_path": "/tmp/logs/service<error>.log",
                },
            )
            config = plistlib.loads(output_path.read_bytes())

        self.assertEqual(config["Label"], LABEL)
        self.assertEqual(
            config["ProgramArguments"],
            [
                "/tmp/home/Applications/A&B.app/Contents/MacOS/App",
                "/tmp/image|gorani/scripts/supervise.sh",
            ],
        )
        self.assertTrue(config["RunAtLoad"])
        self.assertTrue(config["KeepAlive"])
        self.assertEqual(
            config["EnvironmentVariables"]["IMAGE_STORAGE_ROOT"],
            "/Volumes/gorani<images>/image-store",
        )
        self.assertIn(
            "/opt/homebrew/bin",
            config["EnvironmentVariables"]["PATH"],
        )
        self.assertEqual(config["EnvironmentVariables"]["IMAGE_UPLOAD_PORT"], "8090")

    def test_launch_agent_assets_do_not_contain_a_password(self) -> None:
        managed_paths = [
            PROJECT_ROOT / "launchd" / f"{LABEL}.plist.template",
            PROJECT_ROOT / "scripts" / "install-launch-agent.sh",
            PROJECT_ROOT / "scripts" / "supervise-service.sh",
            PROJECT_ROOT / "launchd" / "GoraniImageUploadLauncher.swift",
        ]

        for path in managed_paths:
            contents = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("smb_password", contents, path.name)
            self.assertNotIn("ksj:", contents, path.name)

    def test_app_declares_network_volume_usage(self) -> None:
        info_path = PROJECT_ROOT / "launchd" / "GoraniImageUpload-Info.plist"
        info = plistlib.loads(info_path.read_bytes())

        self.assertEqual(info["CFBundleIdentifier"], LABEL)
        self.assertTrue(info["LSUIElement"])
        self.assertIn("NSNetworkVolumesUsageDescription", info)
        self.assertIn("NSLocalNetworkUsageDescription", info)

    def test_mount_identity_requires_expected_server_and_share(self) -> None:
        mount_output = (
            "//ksj@DESKTOP-0217PLD/gorani-images on /Volumes/gorani-images "
            "(smbfs, nodev, nosuid)\n"
        )
        self.assertTrue(
            mount_matches(
                mount_output,
                "/Volumes/gorani-images",
                "smb://ksj@desktop-0217pld/gorani-images",
            )
        )
        self.assertFalse(
            mount_matches(
                mount_output,
                "/Volumes/gorani-images",
                "smb://ksj@desktop-0217pld/other-share",
            )
        )
        self.assertFalse(
            mount_matches(
                mount_output,
                "/Volumes/gorani-images",
                "smb://ksj@other-server/gorani-images",
            )
        )

    def test_storage_check_rejects_an_unreachable_smb_server(self) -> None:
        mount_output = (
            "//ksj@DESKTOP-0217PLD/gorani-images on /Volumes/gorani-images "
            "(smbfs, nodev, nosuid)\n"
        )
        mount_result = subprocess.CompletedProcess(
            args=["/sbin/mount"],
            returncode=0,
            stdout=mount_output,
            stderr="",
        )

        with patch(
            "upload_service.macos_runtime.subprocess.run",
            return_value=mount_result,
        ), patch(
            "upload_service.macos_runtime.socket.create_connection",
            side_effect=OSError("SMB server is unreachable"),
        ), patch("upload_service.macos_runtime._bounded_stat") as bounded_stat:
            ready, message = check_storage(
                "smb://ksj@DESKTOP-0217PLD/gorani-images",
                Path("/Volumes/gorani-images"),
                Path("/Volumes/gorani-images/image-store"),
                2,
            )

        self.assertFalse(ready)
        self.assertIn("unreachable", message)
        bounded_stat.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "LaunchAgent is macOS-only")
    def test_status_uses_installed_port_and_fails_on_component_errors(self) -> None:
        template_path = PROJECT_ROOT / "launchd" / f"{LABEL}.plist.template"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_home = Path(temp_dir)
            plist_path = temp_home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            render_launch_agent(
                template_path,
                plist_path,
                {
                    "app_executable": "/tmp/GoraniImageUpload",
                    "supervisor_script": "/tmp/supervise-service.sh",
                    "project_root": str(PROJECT_ROOT),
                    "runtime_path": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "smb_host": "unreachable.invalid",
                    "smb_url": "smb://user@unreachable.invalid/images",
                    "mount_point": "/Volumes/not-mounted",
                    "storage_root": "/Volumes/not-mounted/image-store",
                    "upload_host": "0.0.0.0",
                    "upload_port": "8090",
                    "stdout_path": "/tmp/service.log",
                    "stderr_path": "/tmp/service-error.log",
                },
            )
            environment = {**os.environ, "HOME": str(temp_home)}
            status_script = PROJECT_ROOT / "scripts" / "service-status.sh"

            url_result = subprocess.run(
                [status_script, "--health-url"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            status_result = subprocess.run(
                [status_script],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(
            url_result.stdout.strip(),
            "http://127.0.0.1:8090/healthz",
        )
        self.assertEqual(status_result.returncode, 1)
        self.assertIn("SMB: disconnected", status_result.stdout)
        self.assertIn("HTTP: unavailable (http://127.0.0.1:8090/healthz)", status_result.stdout)


if __name__ == "__main__":
    unittest.main()
