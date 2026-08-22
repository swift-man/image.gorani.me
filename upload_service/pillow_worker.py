from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .errors import InvalidImageError
from .image_ops import render_thumbnail_with_pillow


LOGGER = logging.getLogger(__name__)


def main() -> int:
    """Pillow 작업을 별도 프로세스에서 실행해 부모가 제한시간을 강제하게 한다."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("width", type=int)
    parser.add_argument("output_format")
    parser.add_argument("max_pixels", type=int)
    args = parser.parse_args()

    try:
        render_thumbnail_with_pillow(
            args.source,
            args.destination,
            args.width,
            args.output_format,
            args.max_pixels,
        )
    except InvalidImageError as exc:
        # 종료 코드 2는 부모 프로세스가 400 응답으로 변환하는 사용자 입력 오류다.
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        LOGGER.exception("Pillow thumbnail worker failed")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
