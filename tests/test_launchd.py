from __future__ import annotations

import plistlib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LABEL = "me.gorani.image-upload"


class LaunchAgentAssetTests(unittest.TestCase):
    def test_launch_agent_template_has_expected_runtime_contract(self) -> None:
        template_path = PROJECT_ROOT / "launchd" / f"{LABEL}.plist.template"
        rendered = (
            template_path.read_text(encoding="utf-8")
            .replace("__PROJECT_ROOT__", "/tmp/image.gorani.me")
            .replace("__HOME__", "/tmp/home")
            .replace(
                "__APP_EXECUTABLE__",
                "/tmp/home/Applications/GoraniImageUpload.app/Contents/MacOS/"
                "GoraniImageUpload",
            )
            .replace(
                "__RUNTIME_PATH__",
                "/opt/homebrew/opt/postgresql@17/bin:/usr/bin:/bin",
            )
            .replace("__SMB_HOST__", "DESKTOP-0217PLD")
            .replace(
                "__SMB_URL__",
                "smb://ksj@DESKTOP-0217PLD/gorani-images",
            )
            .replace("__MOUNT_POINT__", "/Volumes/gorani-images")
            .replace(
                "__STORAGE_ROOT__",
                "/Volumes/gorani-images/image-store",
            )
        )

        config = plistlib.loads(rendered.encode("utf-8"))

        self.assertEqual(config["Label"], LABEL)
        self.assertEqual(
            config["ProgramArguments"],
            [
                "/tmp/home/Applications/GoraniImageUpload.app/Contents/MacOS/"
                "GoraniImageUpload",
                "/tmp/image.gorani.me/scripts/supervise-service.sh",
            ],
        )
        self.assertTrue(config["RunAtLoad"])
        self.assertTrue(config["KeepAlive"])
        self.assertEqual(
            config["EnvironmentVariables"]["IMAGE_STORAGE_ROOT"],
            "/Volumes/gorani-images/image-store",
        )
        self.assertIn(
            "/opt/homebrew/opt/postgresql@17/bin",
            config["EnvironmentVariables"]["PATH"],
        )

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


if __name__ == "__main__":
    unittest.main()
