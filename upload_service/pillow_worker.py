from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .errors import InvalidImageError
from .image_ops import (
    read_oriented_dimensions_with_pillow,
    render_thumbnail_with_pillow,
)


LOGGER = logging.getLogger(__name__)


def main() -> int:
    """Pillow 작업을 별도 프로세스에서 실행해 부모가 제한시간을 강제하게 한다."""

    parser = argparse.ArgumentParser(add_help=False)
    operations = parser.add_subparsers(dest="operation", required=True)
    inspect_parser = operations.add_parser("inspect", add_help=False)
    inspect_parser.add_argument("source", type=Path)
    inspect_parser.add_argument("max_pixels", type=int)
    render_parser = operations.add_parser("render", add_help=False)
    render_parser.add_argument("source", type=Path)
    render_parser.add_argument("destination", type=Path)
    render_parser.add_argument("width", type=int)
    render_parser.add_argument("output_format")
    render_parser.add_argument("max_pixels", type=int)
    args = parser.parse_args()

    try:
        if args.operation == "inspect":
            width, height = read_oriented_dimensions_with_pillow(
                args.source,
                args.max_pixels,
            )
            print(json.dumps({"width": width, "height": height}))
        else:
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
    raise SystemExit(main())
