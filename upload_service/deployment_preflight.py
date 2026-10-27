"""배포기에 비밀값 없는 고정 종료 코드만 전달하는 사전 검사."""
import os
from pathlib import Path

from .config import load_settings
from .db import Database
from .macos_runtime import check_storage
from .storage import ensure_storage_roots


def main():
    try:
        settings = load_settings()
        smb_url = os.environ["GORANI_SMB_URL"]
        mount_point = Path(os.environ["GORANI_SMB_MOUNT_POINT"])
    except Exception:
        return 41
    try:
        ok, _message = check_storage(smb_url, mount_point, settings.storage_root, 3)
        if not ok:
            return 42
    except Exception:
        return 42
    try:
        ensure_storage_roots(settings)
    except Exception:
        return 43
    try:
        if Database(settings).run_sql("SELECT 1;").strip() != "1":
            return 44
    except Exception:
        return 44
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
