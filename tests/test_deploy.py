from __future__ import annotations

import importlib.util
import http.client
import io
import json
import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request
import urllib.error

from upload_service.server import UploadHandler, UploadHTTPServer


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("deploy_service", ROOT / "scripts/deploy_service.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
OLD = "a" * 40
NEW = "b" * 40


class LaunchctlRefusingPopen(subprocess.Popen):
    """launchctl 실행만 거부하고 다른 명령은 그대로 실행하는 Popen."""

    def __init__(self, args, *other, **options):
        program = options.get("executable") or (
            args if isinstance(args, (str, bytes, os.PathLike)) else args[0])
        if Path(os.fsdecode(program)).name == "launchctl":
            raise AssertionError(f"시험이 실제 launchctl을 실행하려 했습니다: {args!r}")
        super().__init__(args, *other, **options)


def setUpModule():
    # 배포기는 launchctl을 subprocess로만 부른다(서비스 재시작, 설치와 그 되돌리기). 시험이 대역을 빠뜨리거나
    # 회귀로 그 경로에 들어가도 시험을 돌린 맥의 실제 서비스(gui/<uid>/me.gorani.image-upload)를 내리거나
    # 재시작하지 않고 시험이 실패하게 한다. 시험 안에서 Popen이나 run을 대역으로 바꾸면 그 대역이 먼저다.
    guard = patch.object(subprocess, "Popen", LaunchctlRefusingPopen)
    guard.start()
    unittest.addModuleCleanup(guard.stop)


class FakeClock:
    """조회 횟수와 무관하게 sleep으로만 전진하는 테스트 시계."""
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def archive(*members):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as handle:
        for name, content, kind in members:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            member.linkname = "../../outside"
            handle.addfile(member, io.BytesIO(content))
    return data.getvalue()


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old = self.root / "old"
        self.new = self.root / "new"
        self.old.mkdir()
        self.new.mkdir()
        (self.old / ".release-sha").write_text(OLD)
        (self.new / ".release-sha").write_text(NEW)
        deploy.point_current(self.root, self.old)
        deploy.write_json(self.root / "config.json", {
            "environment": {"PATH": "/usr/bin:/bin"}, "health_url": "http://127.0.0.1:1/healthz",
            "guarded": {}, "python": "/usr/bin/python3", "env_file": "/unused/.env",
        })
        self.manager = deploy.Deployer(self.root)

    def test_atomic_write_keeps_private_permissions(self):
        target = self.root / "state.json"
        deploy.atomic_write(target, b"old")
        deploy.atomic_write(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_lock_rejects_second_updater_and_releases_on_exception(self):
        with self.assertRaisesRegex(ValueError, "interrupted"):
            with deploy.deployment_lock(self.root):
                with self.assertRaises(RuntimeError):
                    with deploy.deployment_lock(self.root):
                        self.fail("두 배포가 동시에 잠금을 획득함")
                raise ValueError("interrupted")
        with deploy.deployment_lock(self.root):
            pass

    def test_archive_skips_tracked_venv_and_secrets(self):
        data = archive((".venv/bin/python", b"bad", tarfile.REGTYPE),
                       (".env", b"secret", tarfile.REGTYPE),
                       ("upload_service/a.py", b"source", tarfile.REGTYPE))
        deploy.safe_extract(data, self.new)
        self.assertFalse((self.new / ".venv").exists())
        self.assertFalse((self.new / ".env").exists())
        self.assertEqual((self.new / "upload_service/a.py").read_bytes(), b"source")

    def test_archive_rejects_escape_links_and_special_files(self):
        for name, kind in [("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE),
                           ("pipe", tarfile.FIFOTYPE)]:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                deploy.safe_extract(archive((name, b"x", kind)), self.new)

    def test_inherited_credentials_do_not_override_runtime(self):
        with patch.dict(os.environ, {"PGPASSWORD": "do-not-inherit", "PYTHONPATH": "/bad"}):
            manager = deploy.Deployer(self.root)
        self.assertNotIn("PGPASSWORD", manager.env)
        self.assertNotIn("PYTHONPATH", manager.env)

    def test_failed_health_restores_old_release(self):
        self.manager.restart = Mock()
        self.manager.healthy = Mock(side_effect=[RuntimeError("unhealthy"), None])
        self.manager.preflight = Mock()
        with self.assertRaisesRegex(RuntimeError, "unhealthy"):
            self.manager.activate(self.new, NEW)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertFalse((self.root / "pending.json").exists())
        self.assertEqual(self.manager.restart.call_count, 2)
        self.assertEqual(self.manager.healthy.call_args.args, (OLD,))
        self.assertEqual(json.loads((self.root / "failed.json").read_text())["sha"], NEW)

    def test_failed_activation_is_not_automatically_retried(self):
        deploy.write_json(self.root / "failed.json", {"sha": NEW})
        self.manager.latest = Mock(return_value=NEW)
        self.manager.prepare = Mock()
        self.manager.restart = Mock()
        with self.assertRaisesRegex(RuntimeError, "retry"):
            self.manager.sync()
        self.manager.prepare.assert_not_called()
        self.manager.restart.assert_not_called()

    def test_command_timeout_kills_child_process_group(self):
        process = Mock(pid=123, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired("pip", 1), (b"", b"")]
        with patch.object(deploy.subprocess, "Popen", return_value=process) as popen, \
                patch.object(deploy.os, "killpg") as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.manager.run(["pip", "install", "."], timeout=1)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(123, signal.SIGKILL)

    def test_command_runner_executes_with_private_environment(self):
        result = self.manager.run([sys.executable, "-c", "import os; print(os.getenv('PYTHONNOUSERSITE'))"])
        self.assertEqual(result.strip(), b"1")

    def test_cleanup_timeout_preserves_interruption(self):
        process = Mock(pid=123)
        original = deploy.DeploymentInterrupted()
        process.communicate.side_effect = [original, subprocess.TimeoutExpired("pip", 5)]
        with patch.object(deploy.subprocess, "Popen", return_value=process), \
                patch.object(deploy.os, "killpg"), self.assertRaises(deploy.DeploymentInterrupted) as caught:
            self.manager.run(["pip"])
        self.assertIs(caught.exception, original)

    def test_health_retries_protocol_errors_until_stable(self):
        for phase, error in [("open", http.client.BadStatusLine("invalid")),
                             ("read", http.client.IncompleteRead(b"partial", 10))]:
            with self.subTest(phase=phase):
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.status = 200
                response.headers = {"X-Image-Release": NEW, "X-Image-Pid": "42"}
                response.read.return_value = b'{"status":"ok"}'
                opener = Mock()
                opener.open.return_value = response
                attempts = []

                def transient_error(*_args, **_kwargs):
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise error
                    return response if phase == "open" else b'{"status":"ok"}'

                if phase == "open":
                    opener.open.side_effect = transient_error
                else:
                    response.read.side_effect = transient_error
                clock = FakeClock()
                with patch.object(deploy.urllib.request, "build_opener", return_value=opener), \
                        patch.object(deploy.time, "monotonic", clock.monotonic), \
                        patch.object(deploy.time, "sleep", clock.sleep):
                    self.manager.healthy(NEW)
                self.assertEqual(clock.now, 106)
                self.assertGreaterEqual(len(attempts), 2)

    def test_preflight_accepts_successful_subprocess(self):
        self.manager.run = Mock(return_value=b"")
        self.assertIsNone(self.manager.preflight(self.new))
        self.manager.run.assert_called_once_with(
            [self.new / ".venv/bin/python", "-m", "upload_service.deployment_preflight"],
            cwd=self.new, timeout=30)

    def test_preflight_does_not_reclassify_interruption(self):
        interrupted = deploy.DeploymentInterrupted()
        self.manager.run = Mock(side_effect=interrupted)
        with self.assertRaises(deploy.DeploymentInterrupted) as caught:
            self.manager.preflight(self.new)
        self.assertIs(caught.exception, interrupted)

    def test_preflight_reports_only_fixed_codes(self):
        for code, expected in [(41, "CONFIG"), (42, "SMB"), (43, "STORAGE"),
                               (44, "DATABASE"), (1, "PROCESS")]:
            with self.subTest(code=code):
                self.manager.run = Mock(side_effect=deploy.CommandFailed("secret-command", code))
                with self.assertRaisesRegex(RuntimeError, "^사전 검사 실패: " + expected + "$"):
                    self.manager.preflight(self.new)
        self.manager.run = Mock(side_effect=subprocess.TimeoutExpired("secret-command", 30))
        with self.assertRaisesRegex(RuntimeError, "^사전 검사 실패: TIMEOUT$"):
            self.manager.preflight(self.new)

    def test_real_process_kill_leaves_recoverable_journal(self):
        # 임시 디렉터리만 사용한다. launchd/SMB/운영 DB에는 접근하지 않는다.
        script = '''
import importlib.util, pathlib, signal, sys
spec = importlib.util.spec_from_file_location("deploy", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
manager = module.Deployer(pathlib.Path(sys.argv[2]))
manager.restart = lambda: None
def wait(_sha):
    (manager.root / "ready").touch()
    signal.pause()
manager.healthy = wait
manager.activate(manager.root / "new", sys.argv[3])
'''
        process = subprocess.Popen([sys.executable, "-c", script,
                                    str(ROOT / "scripts/deploy_service.py"), str(self.root), NEW])
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "ready").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("격리 배포 프로세스 준비 실패")
                time.sleep(0.01)
            process.kill()
            process.wait(timeout=5)
            self.assertTrue((self.root / "pending.json").exists())
            self.assertEqual((self.root / "current").resolve(), self.new)
            self.manager.restart = Mock()
            self.manager.healthy = Mock()
            self.manager.recover()
            self.assertEqual((self.root / "current").resolve(), self.old)
            self.assertFalse((self.root / "pending.json").exists())
            self.assertEqual(json.loads((self.root / "failed.json").read_text())["sha"], NEW)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_prepare_uses_hash_lock_and_offline_project_build(self):
        def run(args, **_kwargs):
            if "archive" in args:
                return archive(("requirements-deploy.txt", b"fixture", tarfile.REGTYPE))
            return b""
        self.manager.run = Mock(side_effect=run)
        self.manager.prepare(NEW)
        commands = [call.args[0] for call in self.manager.run.call_args_list]
        installs = [args for args in commands if "install" in args]
        self.assertIn("--require-hashes", installs[0])
        self.assertIn("--only-binary=:all:", installs[0])
        self.assertIn("--no-build-isolation", installs[1])
        self.assertIn("--no-deps", installs[1])

    def test_failed_rollback_preserves_journal_for_next_login(self):
        self.manager.restart = Mock()
        self.manager.healthy = Mock(side_effect=RuntimeError("storage offline"))
        with self.assertRaises(RuntimeError):
            self.manager.activate(self.new, NEW)
        self.assertTrue((self.root / "pending.json").exists())
        with self.assertRaisesRegex(RuntimeError, "먼저 sync"):
            deploy.clear_failed_revision(self.root)
        self.assertTrue((self.root / "failed.json").exists())
        self.manager.healthy = Mock()
        self.manager.recover()
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertFalse((self.root / "pending.json").exists())
        deploy.clear_failed_revision(self.root)
        self.assertFalse((self.root / "failed.json").exists())
        self.manager.latest = Mock(return_value=NEW)
        self.manager.ci_passed = Mock(return_value=True)
        self.manager.prepare = Mock(return_value=self.new)
        self.manager.preflight = Mock()
        self.manager.sync()
        self.assertEqual((self.root / "current").resolve(), self.new)

    def test_success_commits_pointer_only_after_post_start_preflight(self):
        self.manager.restart = Mock()
        self.manager.healthy = Mock()
        self.manager.preflight = Mock(side_effect=RuntimeError("DB down"))
        with self.assertRaises(RuntimeError):
            self.manager.activate(self.new, NEW)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.manager.preflight = Mock()
        self.manager.activate(self.new, NEW)
        self.assertEqual((self.root / "current").resolve(), self.new)
        self.assertFalse((self.root / "pending.json").exists())

    def test_ci_requires_latest_matching_main_push_success(self):
        def run(id, conclusion, sha=NEW, event="push"):
            return {"id": id, "conclusion": conclusion, "head_sha": sha,
                    "event": event, "head_branch": "main"}
        for runs, expected in [([], False), ([run(1, "success", OLD)], False),
                               ([run(1, "success", event="pull_request")], False),
                               ([run(1, "success"), run(2, "failure")], False),
                               ([run(1, "success")], True)]:
            response = io.BytesIO(json.dumps({"workflow_runs": runs}).encode())
            with patch.object(deploy.urllib.request, "urlopen", return_value=response):
                self.assertEqual(self.manager.ci_passed(NEW), expected)

    def test_unavailable_ci_fails_without_preparing_or_switching(self):
        for error in (urllib.error.URLError("offline"),
                      urllib.error.HTTPError(deploy.API, 403, "rate limited", {}, None),
                      urllib.error.HTTPError(deploy.API, 429, "rate limited", {}, None)):
            with self.subTest(error=type(error).__name__):
                self.manager.latest = Mock(return_value=NEW)
                self.manager.prepare = Mock()
                self.manager.restart = Mock()
                with patch.object(deploy.urllib.request, "urlopen", side_effect=error), \
                        self.assertRaises(urllib.error.URLError):
                    self.manager.sync()
                self.manager.prepare.assert_not_called()
                self.manager.restart.assert_not_called()
                self.assertEqual((self.root / "current").resolve(), self.old)

    def test_listener_health_url_handles_wildcards_and_bracketed_ipv6(self):
        for host, target in [("", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"),
                             ("::", "127.0.0.1"), ("localhost", "localhost"),
                             ("::1", "[::1]"), ("2001:db8::15", "[2001:db8::15]")]:
            with self.subTest(host=host):
                self.assertEqual(deploy.health_url_for_environment({
                    "IMAGE_UPLOAD_HOST": host, "IMAGE_UPLOAD_PORT": "8090"}),
                    f"http://{target}:8090/healthz")

    def test_listener_health_url_rejects_invalid_address_or_port(self):
        for host, port in [("host/path", "8090"), ("host@other", "8090"), ("bad:host", "8090"),
                           ("localhost", "0"), ("localhost", "65536"), ("localhost", "bad")]:
            with self.subTest(host=host, port=port), self.assertRaises(RuntimeError):
                deploy.health_url_for_environment({"IMAGE_UPLOAD_HOST": host, "IMAGE_UPLOAD_PORT": port})

    def test_interrupted_activation_rolls_back_then_propagates(self):
        self.manager.restart = Mock()
        self.manager.healthy = Mock(side_effect=[deploy.DeploymentInterrupted(), None])
        with self.assertRaises(deploy.DeploymentInterrupted):
            self.manager.activate(self.new, NEW)
        self.assertEqual((self.root / "current").resolve(), self.old)
        self.assertFalse((self.root / "pending.json").exists())
        self.assertEqual(json.loads((self.root / "failed.json").read_text())["sha"], NEW)

    def test_interrupted_command_kills_its_process_group(self):
        process = Mock(pid=123, returncode=-9)
        process.communicate.side_effect = [deploy.DeploymentInterrupted(), (b"", b"")]
        with patch.object(deploy.subprocess, "Popen", return_value=process), \
                patch.object(deploy.os, "killpg") as killpg, self.assertRaises(deploy.DeploymentInterrupted):
            self.manager.run(["pip", "install", "."])
        killpg.assert_called_once_with(123, signal.SIGKILL)

    def test_pending_ci_keeps_service_unchanged(self):
        self.manager.latest = Mock(return_value=NEW)
        self.manager.ci_passed = Mock(return_value=False)
        self.manager.prepare = Mock()
        self.manager.sync()
        self.manager.prepare.assert_not_called()
        self.assertEqual((self.root / "current").resolve(), self.old)

    def test_new_main_during_build_never_activates_stale_release(self):
        self.manager.latest = Mock(side_effect=[NEW, "c" * 40])
        self.manager.ci_passed = Mock(return_value=True)
        self.manager.prepare = Mock(return_value=self.new)
        self.manager.preflight = Mock()
        self.manager.activate = Mock()
        self.manager.sync()
        self.manager.activate.assert_not_called()
        self.assertFalse(self.new.exists())

    def test_schema_change_blocks_before_installing_dependencies(self):
        self.manager.config["guarded"] = {"sql/schema.sql": "different"}
        self.manager.run = Mock(return_value=archive(("sql/schema.sql", b"changed", tarfile.REGTYPE)))
        with self.assertRaisesRegex(RuntimeError, "sql/schema.sql"):
            self.manager.prepare(NEW)
        self.assertEqual(self.manager.run.call_count, 1)
        self.assertEqual(list((self.root / "releases").iterdir()), [])

    def test_old_process_health_is_not_a_successful_deploy(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.headers = {"X-Image-Release": OLD, "X-Image-Pid": "42"}
        response.read = Mock(return_value=b'{"status":"ok"}')
        opener = Mock()
        opener.open.return_value = response
        with patch.object(deploy.urllib.request, "build_opener", return_value=opener), \
                patch.object(deploy.time, "monotonic", side_effect=[0, 1, 20]), \
                patch.object(deploy.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "시간 초과"):
                self.manager.healthy(NEW, timeout=10)

    def test_sigterm_during_health_is_not_swallowed_as_connection_failure(self):
        for phase in ("open", "read"):
            with self.subTest(phase=phase):
                opener = Mock()
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.status = 200
                response.headers = {"X-Image-Release": NEW, "X-Image-Pid": "42"}
                opener.open.return_value = response

                def interrupt(*_args, **_kwargs):
                    registered_handler.call_args.args[1](signal.SIGTERM, None)

                if phase == "open":
                    opener.open.side_effect = interrupt
                else:
                    response.read.side_effect = interrupt
                previous_umask = os.umask(0o077)
                try:
                    with patch.object(deploy.sys, "argv", ["deploy", "--root", str(self.root), "sync"]), \
                            patch.object(deploy.signal, "signal") as registered_handler, \
                            patch.object(deploy.Deployer, "sync", autospec=True,
                                         side_effect=lambda manager: manager.healthy(NEW)), \
                            patch.object(deploy.urllib.request, "build_opener", return_value=opener), \
                            patch.object(deploy.time, "monotonic", side_effect=[0, 1, 100]), \
                            patch.object(deploy.time, "sleep") as sleep:
                        self.assertEqual(deploy.main(), 143)
                        sleep.assert_not_called()
                        opener.open.assert_called_once()
                finally:
                    os.umask(previous_umask)

    def test_health_headers_leave_json_contract_unchanged(self):
        server = UploadHTTPServer(("127.0.0.1", 0), UploadHandler, Mock(), 2, 3)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"IMAGE_RELEASE_SHA": NEW}):
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz") as response:
                    self.assertEqual(json.load(response), {"status": "ok"})
                    self.assertEqual(response.headers["X-Image-Release"], NEW)
                    self.assertEqual(response.headers["X-Image-Pid"], str(os.getpid()))
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.source = self.home / "source"
        self.source.mkdir()
        for name in deploy.GUARDED:
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture")
        (self.source / ".env").write_text("IMAGE_API_KEYS=test-fixture\n")
        (self.source / ".env").chmod(0o600)
        self.app = self.home / "App"
        self.app.touch()
        self.agent = self.home / "Library/LaunchAgents" / f"{deploy.LABEL}.plist"
        self.agent.parent.mkdir(parents=True)
        self.original = plistlib.dumps({
            "Label": deploy.LABEL, "ProgramArguments": [str(self.app), "/old/supervisor"],
            "WorkingDirectory": str(self.source), "EnvironmentVariables": {
                "GORANI_SMB_URL": "smb://user@host/share", "GORANI_SMB_MOUNT_POINT": "/Volumes/share",
                "IMAGE_UPLOAD_PORT": "8090", "PATH": "/usr/bin:/bin"}})
        self.agent.write_bytes(self.original)
        self.root = self.home / "deployment"
        self.updater = self.agent.with_name(f"{deploy.LABEL}.deploy.plist")
        # 설치가 일찍 멈추는 시험도 실제 홈의 LaunchAgents plist를 읽거나 덮어쓰지 않도록 늘 임시 홈을 쓴다.
        home = patch.object(deploy.Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def test_installer_preserves_environment_and_adds_login_interval_agent(self):
        with patch.object(deploy.sys, "platform", "darwin"), \
                patch.object(deploy.Deployer, "run"), patch.object(deploy.Deployer, "healthy"):
            deploy.install(self.root, self.source)
        service = plistlib.loads(self.agent.read_bytes())
        updater = plistlib.loads(self.updater.read_bytes())
        self.assertEqual(service["WorkingDirectory"], str(self.root / "current"))
        self.assertEqual(service["EnvironmentVariables"], updater["EnvironmentVariables"])
        self.assertEqual((self.root / "original-service.plist").read_bytes(), self.original)
        self.assertTrue(updater["RunAtLoad"])
        self.assertEqual(updater["StartInterval"], 300)
        self.assertNotIn("KeepAlive", updater)
        self.assertEqual((self.root / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-fixture", (self.root / "config.json").read_text())
        self.assertEqual((self.root / "ssh-entry.sh").read_bytes(),
                         (self.source / "scripts/deploy-ssh-entry.sh").read_bytes())
        self.assertEqual((self.root / "ssh-entry.sh").stat().st_mode & 0o777, 0o700)

    def test_old_python_is_rejected_before_installation(self):
        with patch.object(deploy.sys, "platform", "darwin"), \
                patch.object(deploy.sys, "version_info", (3, 9)), \
                self.assertRaisesRegex(RuntimeError, "3.11"):
            deploy.install(self.root, self.source)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.agent.read_bytes(), self.original)

    def test_installer_health_uses_existing_specific_listener_address(self):
        service = plistlib.loads(self.original)
        # 문서용 주소만 사용하며 실제 네트워크/launchd에는 접근하지 않는다.
        service["EnvironmentVariables"]["IMAGE_UPLOAD_HOST"] = "192.0.2.15"
        self.agent.write_bytes(plistlib.dumps(service))
        with patch.object(deploy.sys, "platform", "darwin"), \
                patch.object(deploy.Deployer, "run"), patch.object(deploy.Deployer, "healthy"):
            deploy.install(self.root, self.source)
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual(config["health_url"], "http://192.0.2.15:8090/healthz")
        self.assertEqual(config["environment"]["IMAGE_UPLOAD_HOST"], "192.0.2.15")

    def test_failed_updater_bootstrap_removes_plist_and_restores_original(self):
        def run(_manager, args, **_kwargs):
            if "bootstrap" in args and args[-1] == self.updater:
                raise RuntimeError("bootstrap failed")
        with patch.object(deploy.sys, "platform", "darwin"), \
                patch.object(deploy.Deployer, "run", autospec=True, side_effect=run), \
                patch.object(deploy.Deployer, "healthy"), patch.object(deploy.subprocess, "run") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                deploy.install(self.root, self.source)
        self.assertEqual(self.agent.read_bytes(), self.original)
        self.assertFalse(self.updater.exists())
        self.assertTrue(any(call.args[0][-1].endswith(".deploy") for call in cleanup.call_args_list
                            if isinstance(call.args[0][-1], str)))

    def test_failure_after_service_switch_restores_original_agent(self):
        # 서비스 plist를 바꾸기 시작한 뒤에는 어디서 실패하든(중단 예외 포함) updater 없이 원래 서비스로 되돌린다.
        # 되돌리기는 subprocess.run으로 launchctl을 부르며 bootout의 결과와 상관없이 원래 plist를 다시 띄워야
        # 한다. 앞 단계가 서비스를 내린 채 실패했으면 launchctl bootout은 3으로 끝나므로 대역의 bootout은 늘
        # 실패 코드를 돌려준다.
        write = deploy.atomic_write

        def switch_write_fails_after_replace(path, data, mode=0o600):
            # 교체(os.replace)는 끝났고 그 뒤 디렉터리 fsync가 실패한 경우다.
            write(path, data, mode)
            if path == self.agent and data != self.original:
                raise OSError("fsync failed")

        def service_bootstrap_fails(args, **_kwargs):
            if args[1] == "bootstrap":
                raise deploy.CommandFailed(args[0], 5)

        failures = {
            # 이름: (기대 예외, 실패시킬 대역 이름, 그 대역의 side_effect)
            "health": (RuntimeError, "healthy", RuntimeError("unhealthy")),
            "interrupted": (deploy.DeploymentInterrupted, "healthy", deploy.DeploymentInterrupted()),
            "switch-write": (OSError, "atomic_write", switch_write_fails_after_replace),
            "service-bootstrap": (deploy.CommandFailed, "run", service_bootstrap_fails),
        }
        domain = f"gui/{os.getuid()}"
        for name, (error, target, side_effect) in failures.items():
            with self.subTest(failure=name):
                self.agent.write_bytes(self.original)
                calls, loaded = [], []

                def launchctl(args, **options):
                    calls.append(args)
                    if args[1] == "bootstrap":
                        # 다시 띄우는 시점에 디스크에 있는 plist가 launchd가 읽는 설정이다.
                        loaded.append(Path(args[-1]).read_bytes())
                    result = subprocess.CompletedProcess(args, 3 if args[1] == "bootout" else 0)
                    if options.get("check"):
                        result.check_returncode()
                    return result

                doubles = {"run": Mock(), "healthy": Mock(), "atomic_write": Mock(side_effect=write)}
                doubles[target].side_effect = side_effect
                with patch.object(deploy.sys, "platform", "darwin"), \
                        patch.object(deploy.Deployer, "run", doubles["run"]), \
                        patch.object(deploy.Deployer, "healthy", doubles["healthy"]), \
                        patch.object(deploy, "atomic_write", doubles["atomic_write"]), \
                        patch.object(deploy.subprocess, "run", side_effect=launchctl), \
                        self.assertRaises(error):
                    deploy.install(self.home / f"deployment-{name}", self.source)
                self.assertEqual(self.agent.read_bytes(), self.original)
                self.assertFalse(self.updater.exists())
                self.assertEqual(calls, [["/bin/launchctl", "bootout", f"{domain}/{deploy.LABEL}"],
                                         ["/bin/launchctl", "bootstrap", domain, self.agent]])
                self.assertEqual(loaded, [self.original])

    def test_existing_installation_is_not_overwritten(self):
        self.root.mkdir()
        with patch.object(deploy.sys, "platform", "darwin"), self.assertRaises(RuntimeError):
            deploy.install(self.root, self.source)
        self.assertEqual(self.agent.read_bytes(), self.original)

    def test_preparation_failure_does_not_restart_existing_service(self):
        with patch.object(deploy.sys, "platform", "darwin"), \
                patch.object(deploy, "write_json", side_effect=OSError("disk full")), \
                patch.object(deploy.subprocess, "run") as cleanup, \
                self.assertRaisesRegex(OSError, "disk full"):
            deploy.install(self.root, self.source)
        cleanup.assert_not_called()
        self.assertEqual(self.agent.read_bytes(), self.original)

    def test_missing_plist_fields_are_rejected_before_modifying_service(self):
        for name in ("ProgramArguments", "EnvironmentVariables", "WorkingDirectory", "PATH"):
            with self.subTest(name=name):
                service = plistlib.loads(self.original)
                if name == "PATH":
                    del service["EnvironmentVariables"][name]
                else:
                    del service[name]
                before = plistlib.dumps(service)
                self.agent.write_bytes(before)
                with patch.object(deploy.sys, "platform", "darwin"), \
                        patch.object(deploy.subprocess, "Popen") as popen, \
                        self.assertRaisesRegex(RuntimeError, name):
                    deploy.install(self.root, self.source)
                popen.assert_not_called()
                self.assertFalse(self.root.exists())
                self.assertEqual(self.agent.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
