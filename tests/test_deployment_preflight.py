import contextlib
import io
import itertools
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from upload_service import deployment_preflight as preflight


SSH_ENTRY = Path(__file__).resolve().parents[1] / "scripts/deploy-ssh-entry.sh"
ALLOWED = 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"'


def marker_script(marker, *commands):
    """실행마다 `marker` 에 받은 인자 수를 한 줄씩 덧붙인 뒤 commands 를 도는 sh 스크립트.

    단언과 같은 Path 에서 경로를 만든다. 인자 없이 한 번 실행됐으면 표식은 `0` 한 줄이다.
    """
    return "\n".join(["#!/bin/sh", 'echo "$#" >>' + shlex.quote(str(marker)), *commands, ""])


def run_entry(entry, home, command):
    """설치된 SSH 진입점을 새 세션에서 돌리고, 그 프로세스 그룹이 모두 끝난 뒤 결과를 돌려준다.

    command 가 None 이면 명령 없는 셸 요청처럼 SSH_ORIGINAL_COMMAND 를 두지 않는다.
    진입점이 끝난 뒤에도 뒤에서 도는 설치는 그 뒤에 표식을 남기므로 표식은 그룹이 빈 뒤에 본다.
    setsid 등으로 그룹을 떠난 프로세스는 기다리지 못한다.
    """
    env = {"HOME": str(home)}
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    with subprocess.Popen(["/bin/sh", str(entry)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          cwd="/", env=env, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=5)
            deadline = time.monotonic() + 5
            while True:
                try:
                    os.killpg(process.pid, 0)
                except (ProcessLookupError, PermissionError):
                    # macOS 는 좀비만 남은 그룹에 EPERM 을 돌려준다. 좀비는 더 실행하지 않는다.
                    # 같은 사용자 fixture 전용 판정이다. EPERM만으로 임의 그룹의 종료를 보증하지 않는다.
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("SSH 진입점이 남긴 프로세스가 5초 안에 끝나지 않았습니다. "
                                         f"stdout={stdout!r} stderr={stderr!r}")
                time.sleep(0.01)
        except BaseException:
            # 남은 프로세스가 다음 하위 시험의 표식을 건드리지 않도록 그룹째 멈춘다.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


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
                # 설정을 먼저 읽는다. .env 가 GORANI_SMB_* 를 채울 수 있다(config._load_dotenv).
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
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "Library/Application Support/gorani-image-upload/deploy/sync.sh"
            target.parent.mkdir(parents=True)
            ran = Path(temp) / "sync-ran"
            # 거부한 요청이 출력 없이(또는 뒤에서) 배포를 돌리는 회귀는 표식으로만 드러난다.
            target.write_text(marker_script(ran, "printf 'fixture-sync'"))
            target.chmod(0o700)
            installed_entry = target.with_name("ssh-entry.sh")
            installed_entry.write_bytes(SSH_ENTRY.read_bytes())
            for command in (None, "", "id", ALLOWED + "; id", "scp -t /tmp/file", ALLOWED):
                with self.subTest(command=command):
                    ran.unlink(missing_ok=True)
                    result = run_entry(installed_entry, temp, command)
                    self.assertEqual(result.returncode, 0 if command == ALLOWED else 64)
                    self.assertEqual(result.stdout, "fixture-sync" if command == ALLOWED else "")
                    if command == ALLOWED:
                        self.assertEqual(ran.read_text(), "0\n")
                    else:
                        self.assertFalse(ran.exists())

    def test_custom_root_does_not_execute_default_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            default = home / "Library/Application Support/gorani-image-upload/deploy"
            custom = home / "custom deployment's directory"
            default_marker, custom_marker = home / "wrong-installation", home / "custom-installation"
            for root, marker in [(default, default_marker), (custom, custom_marker)]:
                root.mkdir(parents=True)
                sync = root / "sync.sh"
                # 출력과 함께 표식 파일도 남긴다. 출력을 버리고 기본 설치를 도는 회귀는 표식으로만 드러난다.
                sync.write_text(marker_script(marker, "printf %s " + shlex.quote(marker.name)))
                sync.chmod(0o700)
            installed_entry = custom / "ssh-entry.sh"
            installed_entry.write_bytes(SSH_ENTRY.read_bytes())
            result = run_entry(installed_entry, temp, ALLOWED)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, custom_marker.name)
            # 사용자 지정 설치만 인자 없이 한 번 돌고, 기본 설치는 진입점이 끝난 뒤에도 돌지 않는다.
            self.assertEqual(custom_marker.read_text(), "0\n")
            self.assertFalse(default_marker.exists())

    def test_ssh_entry_preserves_failure_without_default_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            default = home / "Library/Application Support/gorani-image-upload/deploy"
            default.mkdir(parents=True)
            default_sync = default / "sync.sh"
            default_ran, child_ran = home / "default-ran", home / "child-ran"
            # 출력과 함께 표식 파일도 남긴다. 출력을 버리고 기본 설치를 돌거나 권한 없는 파일을 셸로 돌리는 회귀는
            # 종료 코드와 출력만으로는 드러나지 않는다.
            default_sync.write_text(marker_script(default_ran, "printf 'wrong-installation'"))
            default_sync.chmod(0o700)
            custom = home / "custom installation"
            custom.mkdir()
            installed_entry = custom / "ssh-entry.sh"
            installed_entry.write_bytes(SSH_ENTRY.read_bytes())
            sync = custom / "sync.sh"
            states = ("missing", "not-executable", "failed")
            # 매 하위 시험이 파일·권한·표식을 다시 준비하므로 순서와 상관없이 같은 결과여야 한다. 모든 순서를 돌아
            # 앞선 실행이 남긴 파일이나 표식에 기대는 단언이 없는지도 확인한다.
            for order in itertools.permutations(states):
                for state in order:
                    with self.subTest(order=order, state=state):
                        for path in (sync, default_ran, child_ran):
                            path.unlink(missing_ok=True)
                        if state != "missing":
                            sync.write_text(marker_script(child_ran, "printf 'child-started'", "exit 23"))
                            mode = 0o700 if state == "failed" else 0o600
                            sync.chmod(mode)
                            self.assertEqual(sync.stat().st_mode & 0o777, mode)
                        else:
                            self.assertFalse(sync.exists())
                        result = run_entry(installed_entry, temp, ALLOWED)
                        self.assertFalse(default_ran.exists())
                        if state == "failed":
                            self.assertEqual(result.returncode, 23, result.stderr)
                            self.assertEqual(result.stdout, "child-started")
                            self.assertEqual(child_ran.read_text(), "0\n")
                        else:
                            # exec 실패 코드는 셸마다 다르다. 실패와 자식 미실행을 검증한다.
                            self.assertNotEqual(result.returncode, 0)
                            self.assertEqual(result.stdout, "")
                            self.assertFalse(child_ran.exists())
