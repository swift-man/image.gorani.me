from __future__ import annotations

import argparse
import os
import plistlib
import signal
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Sequence, Tuple
from urllib.parse import SplitResult, unquote, urlsplit


def _parse_smb_location(value: str) -> SplitResult:
    normalized = value if value.startswith("smb://") else "smb:" + value
    parsed = urlsplit(normalized)
    if parsed.scheme != "smb" or not parsed.hostname:
        raise ValueError("Invalid SMB location")
    if parsed.password is not None:
        raise ValueError("SMB URL must not include a password; use macOS Keychain")
    if not parsed.path.strip("/"):
        raise ValueError("SMB share is missing")
    return parsed


def smb_host_from_url(value: str) -> str:
    """비밀번호 없는 SMB URL에서 연결 확인에 사용할 호스트를 얻는다."""

    parsed = _parse_smb_location(value)
    assert parsed.hostname is not None
    return parsed.hostname


def _normalized_smb_identity(value: str) -> Tuple[str, str, str]:
    """SMB URL과 mount 원본을 동일한 서버·사용자·공유 식별자로 바꾼다."""

    parsed = _parse_smb_location(value)
    username = unquote(parsed.username or "").lower()
    share = unquote(parsed.path.strip("/")).lower()
    assert parsed.hostname is not None
    return username, parsed.hostname.lower(), share


def find_smb_source(mount_output: str, mount_point: str) -> str | None:
    """mount 출력에서 정확한 마운트 지점의 SMB 원본만 찾는다."""

    marker = f" on {mount_point} (smbfs,"
    for line in mount_output.splitlines():
        if marker in line:
            return line.split(marker, 1)[0]
    return None


def mount_matches(mount_output: str, mount_point: str, smb_url: str) -> bool:
    source = find_smb_source(mount_output, mount_point)
    if source is None:
        return False
    try:
        return _normalized_smb_identity(source) == _normalized_smb_identity(smb_url)
    except ValueError:
        return False


def _is_same_or_child(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _bounded_storage_paths(
    mount_point: Path,
    storage_root: Path,
    timeout_seconds: float,
) -> Tuple[Path, Path]:
    def raise_timeout(_signum, _frame) -> None:
        raise TimeoutError("Storage path resolution timed out")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    try:
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        return mount_point.resolve(strict=True), storage_root.resolve(strict=True)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def check_storage(
    smb_url: str,
    mount_point: Path,
    storage_root: Path,
    timeout_seconds: float,
) -> Tuple[bool, str]:
    """기대 SMB 대상, 서버 연결, 저장소 접근을 모두 제한시간 안에 확인한다."""

    try:
        _username, host, _share = _normalized_smb_identity(smb_url)
        lexical_mount = Path(os.path.abspath(mount_point))
        lexical_storage = Path(os.path.abspath(storage_root))
        if not _is_same_or_child(lexical_mount, lexical_storage):
            return False, "storage root is outside the SMB mount point"

        mount_result = subprocess.run(
            ["/sbin/mount"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if not mount_matches(mount_result.stdout, str(mount_point), smb_url):
            return False, "expected SMB share is not mounted"

        with socket.create_connection((host, 445), timeout=timeout_seconds):
            pass
        actual_mount, actual_storage = _bounded_storage_paths(
            mount_point,
            storage_root,
            timeout_seconds,
        )
        if not _is_same_or_child(actual_mount, actual_storage):
            return False, "storage root resolves outside the SMB mount point"
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as exc:
        return False, str(exc)
    return True, "ready"


def render_launch_agent(
    template_path: Path,
    output_path: Path,
    values: Dict[str, str],
) -> None:
    """환경값을 XML 문자열로 직접 조합하지 않고 plist 구조로 직렬화한다."""

    with template_path.open("rb") as handle:
        config: Dict[str, Any] = plistlib.load(handle)

    smb_host = smb_host_from_url(values["smb_url"])
    lexical_mount = Path(os.path.abspath(values["mount_point"]))
    lexical_storage = Path(os.path.abspath(values["storage_root"]))
    if not _is_same_or_child(lexical_mount, lexical_storage):
        raise ValueError("Storage root must be inside the SMB mount point")
    config["ProgramArguments"] = [
        values["app_executable"],
        values["supervisor_script"],
    ]
    config["WorkingDirectory"] = values["project_root"]
    config["EnvironmentVariables"] = {
        "PATH": values["runtime_path"],
        "GORANI_SMB_HOST": smb_host,
        "GORANI_SMB_URL": values["smb_url"],
        "GORANI_SMB_MOUNT_POINT": values["mount_point"],
        "IMAGE_STORAGE_ROOT": values["storage_root"],
        "IMAGE_UPLOAD_HOST": values["upload_host"],
        "IMAGE_UPLOAD_PORT": values["upload_port"],
    }
    config["StandardOutPath"] = values["stdout_path"]
    config["StandardErrorPath"] = values["stderr_path"]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            plistlib.dump(config, temp_file, fmt=plistlib.FMT_XML, sort_keys=False)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, output_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="macOS LaunchAgent runtime helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check-storage")
    check_parser.add_argument("--smb-url", required=True)
    check_parser.add_argument("--mount-point", type=Path, required=True)
    check_parser.add_argument("--storage-root", type=Path, required=True)
    check_parser.add_argument("--timeout", type=float, default=2.0)
    check_parser.add_argument("--verbose", action="store_true")

    host_parser = subparsers.add_parser("smb-host")
    host_parser.add_argument("--smb-url", required=True)

    render_parser = subparsers.add_parser("render-plist")
    render_parser.add_argument("--template", type=Path, required=True)
    render_parser.add_argument("--output", type=Path, required=True)
    for name in (
        "app-executable",
        "supervisor-script",
        "project-root",
        "runtime-path",
        "smb-url",
        "mount-point",
        "storage-root",
        "upload-host",
        "upload-port",
        "stdout-path",
        "stderr-path",
    ):
        render_parser.add_argument(f"--{name}", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check-storage":
            ready, message = check_storage(
                args.smb_url,
                args.mount_point,
                args.storage_root,
                args.timeout,
            )
            if args.verbose or not ready:
                print(message)
            return 0 if ready else 1

        if args.command == "smb-host":
            print(smb_host_from_url(args.smb_url))
            return 0

        values = {
            key: value
            for key, value in vars(args).items()
            if key not in {"command", "template", "output"}
        }
        render_launch_agent(args.template, args.output, values)
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
