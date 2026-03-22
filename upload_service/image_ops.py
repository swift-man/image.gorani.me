from __future__ import annotations

import mimetypes
import subprocess
from dataclasses import dataclass
from pathlib import Path


ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/tiff": ".tiff",
}

THUMBNAIL_FORMATS = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
}


@dataclass
class ImageInfo:
    content_type: str
    width: int
    height: int
    file_ext: str


def detect_content_type(path: Path) -> str:
    completed = subprocess.run(
        ["file", "--brief", "--mime-type", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def thumbnail_extension(output_format: str) -> str:
    if output_format not in THUMBNAIL_FORMATS:
        raise ValueError(f"Unsupported thumbnail format: {output_format}")
    return THUMBNAIL_FORMATS[output_format]


def inspect_image(path: Path) -> ImageInfo:
    content_type = detect_content_type(path)
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unsupported content type: {content_type}")

    completed = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    width = None
    height = None
    for raw_line in completed.stdout.splitlines():
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


def create_thumbnail(source: Path, destination: Path, width: int, output_format: str) -> ImageInfo:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
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
        check=True,
        capture_output=True,
        text=True,
    )
    return inspect_image(destination)


def guess_download_name(filename: str, fallback_ext: str) -> str:
    guessed_type, guessed_encoding = mimetypes.guess_type(filename)
    if guessed_type and not guessed_encoding:
        return filename
    stem = Path(filename).stem or "upload"
    return stem + fallback_ext
