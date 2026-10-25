#!/usr/bin/env python3
"""로그인 복구와 runner가 함께 사용하는 macOS 단일 호스트 배포기.

운영 checkout/.venv는 수정하지 않는다. 설치 후 이 파일의 안정된 복사본이
커밋별 release를 준비하며, main의 CI 성공과 로컬 준비 검사를 모두 요구한다.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

SOURCE = "https://github.com/swift-man/image.gorani.me.git"
API = "https://api.github.com/repos/swift-man/image.gorani.me"
LABEL = "me.gorani.image-upload"
DEFAULT_ROOT = Path.home() / "Library/Application Support/gorani-image-upload/deploy"
# 자동 적용 중에는 DB 마이그레이션과 네이티브 앱 교체를 하지 않는다.
GUARDED = ("sql/schema.sql", "launchd/GoraniImageUploadLauncher.swift", "scripts/deploy_service.py")
SHA = re.compile(r"[0-9a-f]{40}")


class DeploymentInterrupted(BaseException):
    """네트워크 오류 처리에 흡수되지 않고 정리 경로까지 전달할 종료 신호."""


def health_url_for_environment(environment):
    """기존 수신 주소를 보존한다. wildcard만 로컬 검사 주소로 바꾼다."""
    host = environment.get("IMAGE_UPLOAD_HOST", "127.0.0.1")
    if host in {"", "0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise RuntimeError("IMAGE_UPLOAD_HOST의 주소 형식이 올바르지 않습니다.") from None
        host = f"[{host}]"
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise RuntimeError("IMAGE_UPLOAD_HOST의 주소 형식이 올바르지 않습니다.")
    try:
        port = int(environment["IMAGE_UPLOAD_PORT"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("IMAGE_UPLOAD_PORT는 1~65535의 정수여야 합니다.") from None
    if not 1 <= port <= 65535:
        raise RuntimeError("IMAGE_UPLOAD_PORT는 1~65535의 정수여야 합니다.")
    return f"http://{host}:{port}/healthz"


def log(message):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message, flush=True)


def atomic_write(path, data, mode=0o600):
    """전환 journal을 먼저 디스크에 확정해 강제 종료 후에도 복구할 수 있게 한다."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".deploy-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2).encode())


def point_current(root, target):
    link = root / ".current-next"
    link.unlink(missing_ok=True)
    link.symlink_to(target, target_is_directory=True)
    os.replace(link, root / "current")
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def deployment_lock(root):
    # PID 파일은 재부팅 후 stale 상태가 되므로 프로세스 수명의 flock을 쓴다.
    with (root / "deploy.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("다른 배포가 실행 중입니다. 다음 주기에 재시도합니다.")
        yield


def safe_extract(data, destination):
    """Git에 남아 있는 .venv와 환경 파일은 절대 release로 가져오지 않는다."""
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("안전하지 않은 archive 경로")
            if any(p in {".git", ".venv", "__pycache__"} or p.startswith(".env")
                   for p in path.parts):
                continue
            if not (member.isdir() or member.isfile()):
                raise RuntimeError("release archive의 링크/특수 파일은 허용하지 않습니다.")
            output = destination.joinpath(*path.parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, output.open("xb") as target:
                    shutil.copyfileobj(source, target)
                output.chmod(0o700 if member.mode & 0o111 else 0o600)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Deployer:
    def __init__(self, root):
        self.root = root.resolve(strict=True)
        self.config = json.loads((root / "config.json").read_text())
        # SSH/CI 호출자의 PYTHONPATH, PG*, API key 등이 운영 설정을 덮지 못한다.
        self.env = {"HOME": str(Path.home()), "LANG": "en_US.UTF-8",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        self.env.update(self.config["environment"])
        self.env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL="/dev/null", PYTHONNOUSERSITE="1")

    def run(self, args, cwd=None, timeout=120):
        # 명령 실패 시 출력에 DB/패키지 URL 자격증명이 섞일 수 있어 원문은 로그에 안 남긴다.
        process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            output, _errors = process.communicate(timeout=timeout)
        except BaseException:
            # pip의 빌드 자식도 함께 회수한다. 운영 호스트에서는 DB daemon을 띄우는 테스트를 안 돌린다.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # 정리 대기 실패가 최초 종료 신호나 timeout을 덮지 않게 한다.
                log("프로세스 그룹 종료 후 출력 회수 시간 초과")
            raise
        if process.returncode:
            raise RuntimeError(f"명령 실패: {Path(str(args[0])).name} (exit {process.returncode})")
        return output

    def latest(self):
        cache = self.root / "source.git"
        if not cache.exists():
            self.run(["git", "init", "--bare", cache])
        self.run(["git", "--git-dir", cache, "fetch", "--no-tags", SOURCE,
                  "+refs/heads/main:refs/heads/main"])
        sha = self.run(["git", "--git-dir", cache, "rev-parse", "refs/heads/main"]).decode().strip()
        if not SHA.fullmatch(sha):
            raise RuntimeError("올바르지 않은 main revision")
        return sha

    def ci_passed(self, sha):
        request = urllib.request.Request(
            f"{API}/actions/workflows/ci.yml/runs?branch=main&event=push&head_sha={sha}&per_page=20",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "gorani-image-deploy"})
        with urllib.request.urlopen(request, timeout=15) as response:
            runs = json.load(response)["workflow_runs"]
        # 재실행된 최신 run의 상태를 기준으로 하며, 다른 커밋 성공을 재사용하지 않는다.
        matching = [r for r in runs if r["head_sha"] == sha and r["event"] == "push"
                    and r["head_branch"] == "main"]
        return bool(matching) and max(matching, key=lambda r: r["id"])["conclusion"] == "success"

    def prepare(self, sha):
        if shutil.disk_usage(self.root).free < 2 * 1024 ** 3:
            raise RuntimeError("배포 디스크 여유가 2 GiB 미만입니다. 과거 release를 정리하세요.")
        releases = self.root / "releases"
        releases.mkdir(exist_ok=True)
        release = Path(tempfile.mkdtemp(prefix=sha + "-", dir=releases))
        try:
            archive = self.run(["git", "--git-dir", self.root / "source.git", "archive", sha])
            safe_extract(archive, release)
            for name, expected in self.config["guarded"].items():
                if digest(release / name) != expected:
                    raise RuntimeError(f"수동 업그레이드가 필요한 파일 변경: {name}")
            self.run([self.config["python"], "-m", "venv", release / ".venv"])
            python = release / ".venv/bin/python"
            self.run([python, "-m", "pip", "install", "--disable-pip-version-check", "."],
                     cwd=release, timeout=600)
            self.run([python, "-m", "pip", "check"], cwd=release)
            # 전체 회귀/DB daemon 시험은 CI에서만 실행한다. 여기서는 .env 연결 전 구문 검증만 한다.
            self.run([python, "-m", "compileall", "-q", "upload_service"], cwd=release)
            atomic_write(release / ".release-sha", sha.encode())
            (release / ".env").symlink_to(self.config["env_file"])
            return release
        except BaseException:
            shutil.rmtree(release)
            raise

    def preflight(self, release):
        # 별도 프로세스의 제한시간으로 응답 없는 SMB/psql도 전체 배포를 붙잡지 못한다.
        self.run([release / ".venv/bin/python", "-c", """
import os
from pathlib import Path
from upload_service.config import load_settings
from upload_service.db import Database
from upload_service.macos_runtime import check_storage
from upload_service.storage import ensure_storage_roots
s = load_settings()
ok, message = check_storage(os.environ['GORANI_SMB_URL'],
    Path(os.environ['GORANI_SMB_MOUNT_POINT']), s.storage_root, 3)
if not ok:
    raise RuntimeError(message)
ensure_storage_roots(s)
if Database(s).run_sql('SELECT 1;').strip() != '1':
    raise RuntimeError('Database readiness failed')
"""], cwd=release, timeout=30)

    def restart(self):
        self.run(["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], timeout=30)

    def healthy(self, sha, timeout=60):
        deadline = time.monotonic() + timeout
        stable_pid, stable_since = None, 0
        # proxy 환경변수를 무시하고 설치된 서비스의 수신 주소를 검사한다.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            try:
                with opener.open(self.config["health_url"], timeout=2) as response:
                    pid = response.headers.get("X-Image-Pid")
                    matches = response.status == 200 and json.load(response) == {"status": "ok"}
                    matches = matches and bool(pid and pid.isdigit())
                    matches = matches and response.headers.get("X-Image-Release") == sha
                    if matches:
                        # 동일 PID가 5초 이상 살아 있어야 재시작 루프를 성공으로 오판하지 않는다.
                        if pid != stable_pid or not stable_since:
                            stable_pid, stable_since = pid, time.monotonic()
                        if time.monotonic() - stable_since >= 5:
                            return
                    else:
                        stable_since = 0
            except (OSError, ValueError, http.client.HTTPException):
                stable_since = 0
            time.sleep(1)
        raise RuntimeError("서비스 revision/health 확인 시간 초과")

    def recover(self):
        journal = self.root / "pending.json"
        if journal.exists():
            previous = json.loads(journal.read_text())
            # 같은 실패 revision 때문에 정상 서비스가 5분마다 중단되는 것을 막는다.
            write_json(self.root / "failed.json", {"sha": previous["candidate_sha"]})
            log("미완료 전환을 이전 release로 복구합니다.")
            point_current(self.root, previous["path"])
            self.restart()
            self.healthy(previous["sha"])
            journal.unlink()

    def activate(self, release, sha):
        previous = (self.root / "current").resolve(strict=True)
        marker = previous / ".release-sha"
        write_json(self.root / "pending.json", {
            "path": str(previous), "sha": marker.read_text().strip() if marker.exists() else "unmanaged",
            "candidate_sha": sha})
        try:
            point_current(self.root, release)
            self.restart()
            self.healthy(sha)
            self.preflight(release)
        except BaseException:
            self.recover()
            raise
        (self.root / "pending.json").unlink()
        log(f"배포 완료: {sha}")

    def sync(self):
        self.recover()
        sha = self.latest()
        failure = self.root / "failed.json"
        if failure.exists() and json.loads(failure.read_text())["sha"] == sha:
            raise RuntimeError(f"이전 활성화가 실패한 revision입니다. 원인 수정 후 retry가 필요합니다: {sha}")
        current = (self.root / "current").resolve(strict=True)
        marker = current / ".release-sha"
        if marker.exists() and marker.read_text().strip() == sha:
            self.preflight(current)
            self.healthy(sha, timeout=10)
            log(f"최신 release 정상: {sha}")
            return
        if not self.ci_passed(sha):
            log(f"적용 보류: main CI가 아직 성공하지 않았습니다. {sha}")
            return
        self.preflight(current)
        candidate = self.prepare(sha)
        try:
            self.preflight(candidate)
            # 설치 중 새 main이 생긴 경우 뒤처진 커밋을 전환하지 않는다.
            if self.latest() != sha:
                log("main이 변경되었습니다. 다음 주기에 최신 버전을 준비합니다.")
                return
            self.activate(candidate, sha)
        finally:
            # 운영/복구 대상은 삭제하지 않는다. 과거 성공 release 정리는 명시적인 운영 작업이다.
            if (self.root / "current").resolve() != candidate.resolve() and not (self.root / "pending.json").exists():
                shutil.rmtree(candidate)


def install(root, source):
    """기존 서비스 설정/키체인 주체를 보존하고 별도 updater LaunchAgent만 추가한다."""
    if sys.platform != "darwin":
        raise RuntimeError("macOS에서만 설치할 수 있습니다.")
    if root.exists():
        raise RuntimeError("배포 디렉터리가 이미 있습니다. 기존 설정을 덮어쓰지 않습니다.")
    source = source.resolve(strict=True)
    env_file = source / ".env"
    if not env_file.is_file() or env_file.stat().st_mode & 0o077:
        raise RuntimeError("운영 .env가 필요하며 권한은 600이어야 합니다.")
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    original = agent.read_bytes()
    service = plistlib.loads(original)
    if not isinstance(service, dict):
        raise RuntimeError("서비스 LaunchAgent plist는 dictionary 형식이어야 합니다.")
    required = {"ProgramArguments", "EnvironmentVariables", "WorkingDirectory"}
    missing = required - service.keys()
    if missing:
        raise RuntimeError("서비스 LaunchAgent 필수 키 누락: " + ", ".join(sorted(missing)))
    arguments = service["ProgramArguments"]
    if not isinstance(arguments, list) or not arguments or not isinstance(arguments[0], str):
        raise RuntimeError("서비스 LaunchAgent ProgramArguments의 앱 경로가 올바르지 않습니다.")
    app = service["ProgramArguments"][0]
    if not Path(app).is_file():
        raise RuntimeError("먼저 install-launch-agent.sh로 네이티브 앱을 설치하세요.")
    environment = service["EnvironmentVariables"]
    if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()):
        raise RuntimeError("서비스 LaunchAgent EnvironmentVariables는 문자열 dictionary여야 합니다.")
    required = {"GORANI_SMB_URL", "GORANI_SMB_MOUNT_POINT", "IMAGE_UPLOAD_PORT", "PATH"}
    missing = {name for name in required if not environment.get(name)}
    if missing:
        raise RuntimeError("서비스 LaunchAgent 필수 환경변수 누락: " + ", ".join(sorted(missing)))
    health_url = health_url_for_environment(environment)
    if service["WorkingDirectory"] != str(source):
        raise RuntimeError("현재 운영 checkout에서 설치하세요. 별도 개발 worktree는 사용할 수 없습니다.")
    updater = agent.with_name(f"{LABEL}.deploy.plist")
    if updater.exists():
        raise RuntimeError("기존 updater plist가 있습니다. 먼저 중지/백업하세요.")
    root.mkdir(parents=True, mode=0o700)
    config = {
        "python": str(Path(sys._base_executable).resolve()),
        "env_file": str(env_file), "environment": environment,
        "health_url": health_url,
        "guarded": {name: digest(source / name) for name in GUARDED},
    }
    domain = f"gui/{os.getuid()}"
    service_change_started = False
    updater_change_started = False
    try:
        write_json(root / "config.json", config)
        atomic_write(root / "original-service.plist", original)
        atomic_write(root / "manager.py", Path(__file__).read_bytes())
        script = "#!/bin/zsh\nset -eu\nexec " + " ".join(shlex.quote(s) for s in [
            config["python"], str(root / "manager.py"), "--root", str(root), "sync"]) + "\n"
        atomic_write(root / "auto-deploy.sh", script.encode(), 0o700)
        wrapper = "#!/bin/zsh\nset -eu\nexec " + shlex.quote(app) + " " + shlex.quote(str(root / "auto-deploy.sh")) + "\n"
        atomic_write(root / "sync.sh", wrapper.encode(), 0o700)
        point_current(root, source)
        service["ProgramArguments"] = [app, str(root / "current/scripts/supervise-service.sh")]
        service["WorkingDirectory"] = str(root / "current")
        # replace 후 fsync 실패도 복구해야 하므로 쓰기를 시도하기 전에 표시한다.
        service_change_started = True
        atomic_write(agent, plistlib.dumps(service))
        deployed = Deployer(root)
        deployed.run(["/bin/launchctl", "bootout", f"{domain}/{LABEL}"])
        deployed.run(["/bin/launchctl", "bootstrap", domain, agent])
        deployed.healthy("unmanaged")
        updater_change_started = True
        atomic_write(updater, plistlib.dumps({
            "Label": f"{LABEL}.deploy", "ProgramArguments": [app, str(root / "auto-deploy.sh")],
            "WorkingDirectory": str(root), "EnvironmentVariables": environment,
            "RunAtLoad": True, "StartInterval": 300, "ProcessType": "Background",
            "StandardOutPath": str(root / "deploy.log"),
            "StandardErrorPath": str(root / "deploy-error.log"),
        }))
        deployed.run(["/bin/launchctl", "bootstrap", domain, updater])
    except BaseException:
        # 설정을 되돌리되 실패 자료는 보존한다. 재설치 전 로그/복구 상태를 확인한다.
        if updater_change_started:
            subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{LABEL}.deploy"], capture_output=True)
            updater.unlink(missing_ok=True)
        if service_change_started:
            atomic_write(agent, original)
            subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
            subprocess.run(["/bin/launchctl", "bootstrap", domain, agent], capture_output=True)
        raise
    log("자동 배포 설치 완료. 로그인 시 및 5분 간격으로 main을 확인합니다.")


def clear_failed_revision(root):
    if (root / "pending.json").exists():
        raise RuntimeError("미완료 복구가 있습니다. 먼저 sync로 이전 서비스를 복구한 뒤 retry하세요.")
    (root / "failed.json").unlink(missing_ok=True)
    log("실패 revision 차단을 해제했습니다. 다음 updater 실행에서 다시 검사합니다.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("command", choices=("install", "sync", "retry"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    args.root = args.root.expanduser().resolve()
    os.umask(0o077)
    def interrupted(_signum, _frame):
        raise DeploymentInterrupted("배포 중단 요청")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.command == "install":
            install(args.root, args.source)
        else:
            with deployment_lock(args.root):
                if args.command == "retry":
                    clear_failed_revision(args.root)
                else:
                    Deployer(args.root).sync()
    except DeploymentInterrupted:
        log("배포 중단 요청을 처리했습니다.")
        return 128 + signal.SIGTERM
    except Exception as exc:
        log(f"자동 배포 실패: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
