from __future__ import annotations

import mimetypes
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path

from .errors import ImageProcessingTimeoutError


ALLOWED_CONTENT_TYPES = {
    # 원본 업로드로 허용할 MIME 타입과 내부 확장자 매핑이다.
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/tiff": ".tiff",
}

THUMBNAIL_FORMATS = {
    # 현재 환경에서 썸네일 출력 시 사용할 수 있는 확장자 매핑이다.
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
}


@dataclass
class ImageInfo:
    """이미지에서 추출한 핵심 메타데이터."""

    content_type: str
    width: int
    height: int
    file_ext: str


def _run_image_command(
    command: list[str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImageProcessingTimeoutError(
            f"Image command exceeded {timeout_seconds} seconds"
        ) from exc


def detect_content_type(path: Path, timeout_seconds: int = 30) -> str:
    # 확장자 대신 실제 파일 내용을 기준으로 MIME 타입을 판단한다.
    completed = _run_image_command(
        ["file", "--brief", "--mime-type", str(path)],
        timeout_seconds,
    )
    return completed.stdout.strip()


def thumbnail_extension(output_format: str) -> str:
    # 파일명 생성 시 포맷과 확장자가 일관되게 맞도록 분리해둔다.
    if output_format not in THUMBNAIL_FORMATS:
        raise ValueError(f"Unsupported thumbnail format: {output_format}")
    return THUMBNAIL_FORMATS[output_format]


def inspect_image(path: Path, timeout_seconds: int = 30) -> ImageInfo:
    # sips로 픽셀 크기를 읽고, file 명령으로 MIME 타입을 검증한다.
    content_type = detect_content_type(path, timeout_seconds)
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unsupported content type: {content_type}")

    completed = _run_image_command(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        timeout_seconds,
    )
    width = None
    height = None
    for raw_line in completed.stdout.splitlines():
        # macOS sips 출력은 "pixelWidth: 123" 형태라서 직접 파싱한다.
        line = raw_line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    if width is None or height is None:
        raise ValueError(f"Unable to inspect image dimensions for {path}")
    return ImageInfo(
        content_type=content_type,
        width=width,
        height=height,
        file_ext=ALLOWED_CONTENT_TYPES[content_type],
    )


def create_thumbnail(
    source: Path,
    destination: Path,
    width: int,
    output_format: str,
    timeout_seconds: int = 30,
    max_pixels: int = 40_000_000,
) -> ImageInfo:
    # 썸네일 대상 폴더는 미리 준비해두고, 포맷에 맞는 인코더로 리사이즈+변환을 한다.
    destination.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "webp":
        return create_thumbnail_with_pillow(
            source,
            destination,
            width,
            output_format,
            timeout_seconds,
            max_pixels,
        )
    _run_image_command(
        [
            "sips",
            "-s",
            "format",
            output_format,
            "--resampleWidth",
            str(width),
            str(source),
            "--out",
            str(destination),
        ],
        timeout_seconds,
    )
    return inspect_image(destination, timeout_seconds)


def create_thumbnail_with_pillow(
    source: Path,
    destination: Path,
    width: int,
    output_format: str,
    timeout_seconds: int = 30,
    max_pixels: int = 40_000_000,
) -> ImageInfo:
    # 이 macOS 환경의 sips는 WebP 출력을 안정적으로 못 해서 Pillow 인코더를 사용한다.
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "WebP thumbnail generation requires Pillow. Install project dependencies first."
        ) from exc

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.width * image.height > max_pixels:
                    raise ValueError(f"Image exceeds max pixel count of {max_pixels}")
                working = image.convert("RGBA")
                target_height = max(1, round((working.height * width) / working.width))
                resized = working.resize((width, target_height), Image.Resampling.LANCZOS)
                resized.save(
                    destination,
                    format=output_format.upper(),
                    quality=85,
                    method=6,
                    lossless=False,
                )
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f"Image exceeds safe decoder pixel limits: {source}") from exc
    return inspect_image(destination, timeout_seconds)


def guess_download_name(filename: str, fallback_ext: str) -> str:
    # 다운로드용 원본 이름은 사용자 이름을 최대한 살리되 확장자는 보정한다.
    basename = Path(filename.replace("\\", "/")).name.replace("\x00", "").strip()
    if not basename:
        basename = "upload" + fallback_ext
    guessed_type, guessed_encoding = mimetypes.guess_type(basename)
    if guessed_type and not guessed_encoding:
        download_name = basename
    else:
        stem = Path(basename).stem or "upload"
        download_name = stem + fallback_ext

    # 메타데이터와 psql 명령 인자가 비정상적으로 커지지 않도록 이름 길이를 제한한다.
    suffix = Path(download_name).suffix
    max_stem_length = max(1, 200 - len(suffix))
    return Path(download_name).stem[:max_stem_length] + suffix
