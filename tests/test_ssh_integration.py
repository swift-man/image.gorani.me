"""임시 키/설정과 loopback sshd로 강제 명령을 검증한다.

IMAGE_TEST_SSH=1일 때만 실행한다. opt-in 후 도구/인증 실패는 skip이 아닌 실패다.
운영 sshd, 사용자 authorized_keys/known_hosts 및 배포 스크립트는 사용하지 않는다.
"""
import os
from pathlib import Path
import pwd
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ALLOWED = 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"'


def ssh_log_tail(directory):
    """임시 서버 로그만 제한된 크기로 읽어 최초 실패의 진단을 보조한다."""
    try:
        with (directory / "sshd.log").open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            content = stream.read(4096).decode("utf-8", errors="replace")
    except OSError:
        # 파일 경로/예외 내용을 추가로 노출하거나 원래 실패를 덮지 않는다.
        content = "[log unavailable]"
    return "\nIsolated sshd log (last 4096 bytes):\n" + (content or "[empty log]")


@unittest.skipUnless(os.environ.get("IMAGE_TEST_SSH") == "1", "isolated SSH integration is opt-in")
class SSHIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if os.getuid() == 0:
            raise RuntimeError("Run isolated SSH tests as a non-root user; do not use sudo")
        cls.ssh = shutil.which("ssh")
        keygen = shutil.which("ssh-keygen")
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        if not cls.ssh or not keygen or not Path(sshd).is_file():
            raise RuntimeError("SSH integration requires ssh, ssh-keygen and sshd")
        cls.temp = tempfile.TemporaryDirectory(prefix="image-ssh-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name).resolve()
        cls.environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
        for name in ("host", "client", "untrusted"):
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(cls.directory / name)],
                           env=cls.environment, check=True, capture_output=True, timeout=10)
        entry = cls.directory / "ssh-entry.sh"
        entry.write_bytes((ROOT / "scripts/deploy-ssh-entry.sh").read_bytes())
        sync = cls.directory / "sync.sh"
        # 이 파일만 실행한다. 실제 배포기나 운영 서비스에는 접근하지 않는다.
        sync.write_text("#!/bin/sh\nprintf 'isolated-sync'\nexit 23\n")
        sync.chmod(0o700)
        # 경로 처리 회귀가 있어도 실제 운영 홈의 배포기를 실행하지 않도록 한정한다.
        home = cls.directory / "home"
        home.mkdir()
        forced = "/usr/bin/env HOME=" + shlex.quote(str(home)) + " /bin/sh " + shlex.quote(str(entry))
        forced = forced.replace("\\", "\\\\").replace('"', '\\"')
        public_key = (cls.directory / "client.pub").read_text().strip()
        authorized = cls.directory / "authorized_keys"
        authorized.write_text(f'restrict,command="{forced}" {public_key}\n')
        authorized.chmod(0o600)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        host_key = " ".join((cls.directory / "host.pub").read_text().split()[:2])
        (cls.directory / "known_hosts").write_text(f"[127.0.0.1]:{cls.port} {host_key}\n")
        cls.username = pwd.getpwuid(os.getuid()).pw_name
        config = cls.directory / "sshd_config"
        config.write_text(f'''Port {cls.port}
ListenAddress 127.0.0.1
HostKey "{cls.directory / 'host'}"
PidFile "{cls.directory / 'pid'}"
AuthorizedKeysFile "{authorized}"
AllowUsers {cls.username}
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes yes
PermitRootLogin no
AllowTcpForwarding yes
PermitTTY yes
Subsystem sftp internal-sftp
LoginGraceTime 5
LogLevel VERBOSE
''')
        # 클라이언트 설정과 서버 설정은 모두 임시 파일로 고정한다.
        log = (cls.directory / "sshd.log").open("wb")
        cls.addClassCleanup(log.close)
        cls.server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)],
                                      env=cls.environment, stdout=log, stderr=log)
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 10
        while True:
            if cls.server.poll() is not None:
                raise RuntimeError("isolated sshd exited" + ssh_log_tail(cls.directory))
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2) as connection:
                    if connection.recv(256).startswith(b"SSH-"):
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("isolated sshd startup timed out" + ssh_log_tail(cls.directory))
            time.sleep(0.05)

    @classmethod
    def stop_server(cls):
        if cls.server.poll() is None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.server.kill()
                cls.server.wait(timeout=5)

    def _formatMessage(self, msg, standardMsg):
        # unittest의 단언 실패 경로에서만 읽는다. 성공/예상된 SSH 거부는 출력하지 않는다.
        return super()._formatMessage(msg, standardMsg) + ssh_log_tail(self.directory)

    def request(self, command=None, *, key="client", options=()):
        args = [
            self.ssh, "-F", "/dev/null", "-T", "-p", str(self.port),
            "-i", str(self.directory / key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.directory / 'known_hosts'}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=3",
            *options, f"{self.username}@127.0.0.1", *([] if command is None else [command]),
        ]
        try:
            return subprocess.run(args, env=self.environment, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError) as error:
            # 타입/종료 원인은 보존하고 Python 3.11 traceback의 부가 설명만 붙인다.
            error.add_note(ssh_log_tail(self.directory))
            raise

    def test_allowed_command_preserves_remote_exit(self):
        result = self.request(ALLOWED)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(result.stdout, "isolated-sync")

    def test_other_commands_are_denied_by_forced_entry(self):
        for command in ("", "id", ALLOWED + "; id", "sftp", "scp -t /tmp/file"):
            with self.subTest(command=command):
                result = self.request(command)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_shell_request_without_command_is_denied(self):
        result = self.request()
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unregistered_key_cannot_authenticate(self):
        result = self.request(ALLOWED, key="untrusted")
        self.assertEqual(result.returncode, 255, result.stderr)
        self.assertIn("Permission denied", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_restricted_key_cannot_open_remote_forward(self):
        result = self.request(ALLOWED, options=("-o", "ExitOnForwardFailure=yes", "-R", "0:127.0.0.1:9"))
        self.assertEqual(result.returncode, 255, result.stderr)
        self.assertIn("remote port forwarding failed", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_restricted_key_cannot_open_direct_forward(self):
        result = self.request(ALLOWED, options=("-W", "127.0.0.1:9"))
        self.assertEqual(result.returncode, 255, result.stderr)
        self.assertIn("administratively prohibited", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_restricted_key_cannot_allocate_pty(self):
        result = self.request(ALLOWED, options=("-tt",))
        self.assertIn("PTY allocation request failed", result.stderr)
        # OpenSSH는 PTY 거절 시 즉시 끝나거나 PTY 없이 강제 명령을 계속할 수 있다.
        if result.returncode == 255:
            self.assertEqual(result.stdout, "")
        else:
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(result.stdout, "isolated-sync")

    def test_subsystem_request_is_denied(self):
        result = self.request("sftp", options=("-s",))
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertEqual(result.stdout, "")


class SSHDiagnosticsTests(unittest.TestCase):
    """sshd 없이도 실패 진단 자체의 크기 제한과 원인 보존을 검사한다."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="image-ssh-diagnostics-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "sshd.log"

    def test_tail_is_bounded_and_keeps_latest_bytes(self):
        self.log.write_bytes(b"discarded-prefix" + b"x" * 4096 + b"latest")
        result = ssh_log_tail(self.directory)
        self.assertNotIn("discarded-prefix", result)
        self.assertTrue(result.endswith("x" * 4090 + "latest"))
        self.assertEqual(len(result.split("\n", 2)[2]), 4096)

    def test_missing_empty_and_invalid_utf8_logs(self):
        self.assertIn("[log unavailable]", ssh_log_tail(self.directory))
        self.log.write_bytes(b"")
        self.assertIn("[empty log]", ssh_log_tail(self.directory))
        self.log.write_bytes(b"\xfflatest")
        self.assertTrue(ssh_log_tail(self.directory).endswith("\ufffdlatest"))

    def test_assertion_failure_preserves_message_and_adds_log(self):
        self.log.write_text("diagnostic-marker")
        case = SSHIntegrationTests("test_allowed_command_preserves_remote_exit")
        case.directory = self.directory
        with self.assertRaises(AssertionError) as caught:
            case.assertEqual(1, 2, "original-message")
        self.assertIn("original-message", str(caught.exception))
        self.assertIn("diagnostic-marker", str(caught.exception))
        with mock.patch(__name__ + ".ssh_log_tail") as read_log:
            case.assertEqual(1, 1)
            read_log.assert_not_called()

    def test_client_errors_keep_identity_and_gain_log_note(self):
        case = SSHIntegrationTests("test_allowed_command_preserves_remote_exit")
        case.directory = self.directory
        case.ssh, case.port, case.username, case.environment = "ssh", 12345, "fixture", {}
        for error in (subprocess.TimeoutExpired("ssh", 10), OSError("client unavailable")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("subprocess.run", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        case.request(ALLOWED)
                self.assertIs(caught.exception, error)
                self.assertIn("[log unavailable]", error.__notes__[0])
