from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, List

from .config import Settings
from .db import AssetRecord, VariantRecord
from .image_ops import ImageInfo, create_thumbnail, guess_download_name, inspect_image, thumbnail_extension


@dataclass
class StoredAsset:
    """원본과 파생 이미지 묶음을 한 번에 들고 다니는 값 객체."""

    asset: AssetRecord
    variants: List[VariantRecord]


def sha256_file(path: Path) -> str:
    # 대용량 파일도 처리할 수 있게 청크 단위로 해시를 계산한다.
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_segments(sha256: str) -> tuple[str, str]:
    # 상위 디렉터리 쏠림을 막기 위해 앞 두 세그먼트로 폴더를 나눈다.
    return sha256[:2], sha256[2:4]


def _public_url(settings: Settings, category: str, sha256: str, filename: str) -> str:
    # Nginx가 그대로 서빙할 공개 URL 규칙을 이 함수 하나에 모은다.
    first, second = _hash_segments(sha256)
    return f"{settings.public_prefix}/{category}/{first}/{second}/{filename}"


def _storage_path(root: Path, sha256: str, filename: str) -> Path:
    # 디스크 경로도 공개 URL과 동일한 해시 세그먼트를 사용한다.
    first, second = _hash_segments(sha256)
    return root / first / second / filename


def ensure_storage_roots(settings: Settings) -> None:
    # 서버 시작 시 원본/파생 이미지 루트가 없으면 생성한다.
    settings.original_root.mkdir(parents=True, exist_ok=True)
    settings.variants_root.mkdir(parents=True, exist_ok=True)


def stage_upload(file_obj, original_filename: str, max_bytes: int) -> tuple[Path, int]:
    # 업로드는 먼저 임시 파일에 받고, 크기 제한을 넘으면 즉시 중단한다.
    total = 0
    suffix = Path(original_filename).suffix or ".upload"
    with NamedTemporaryFile(prefix="upload-", suffix=suffix, delete=False) as temp_file:
        while True:
            chunk = file_obj.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Upload exceeds max size of {max_bytes} bytes")
            temp_file.write(chunk)
        return Path(temp_file.name), total


def finalize_store(temp_path: Path, destination: Path) -> None:
    # 최종 경로에는 원자적 rename으로만 올려 반쯤 쓴 파일 노출을 막는다.
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # 동일 해시 파일이 이미 있으면 새 임시 파일은 버린다.
        temp_path.unlink(missing_ok=True)
        return

    temp_destination = destination.with_suffix(destination.suffix + ".tmp")
    shutil.move(str(temp_path), str(temp_destination))
    os.replace(temp_destination, destination)


def build_asset_record(
    settings: Settings,
    original_filename: str,
    temp_path: Path,
    byte_size: int,
) -> StoredAsset:
    # 원본 검사 -> 해시 계산 -> 최종 저장 -> 파생 이미지 생성 순서로 진행한다.
    image_info = inspect_image(temp_path)
    safe_name = guess_download_name(original_filename, image_info.file_ext)
    sha256 = sha256_file(temp_path)
    original_filename_on_disk = f"{sha256}{image_info.file_ext}"
    original_path = _storage_path(settings.original_root, sha256, original_filename_on_disk)
    finalize_store(temp_path, original_path)

    asset = AssetRecord(
        sha256=sha256,
        original_filename=safe_name,
        content_type=image_info.content_type,
        file_ext=image_info.file_ext,
        byte_size=byte_size,
        width=image_info.width,
        height=image_info.height,
        storage_path=str(original_path),
        public_url=_public_url(settings, "original", sha256, original_filename_on_disk),
    )

    variants: List[VariantRecord] = []
    if settings.enable_thumbnails:
        # 썸네일은 고정된 폭 목록만 생성한다.
        variants.extend(generate_variants(settings, asset, image_info))

    return StoredAsset(asset=asset, variants=variants)


def generate_variants(
    settings: Settings,
    asset: AssetRecord,
    image_info: ImageInfo,
) -> Iterable[VariantRecord]:
    # 원본보다 큰 썸네일은 만들지 않고, 이미 있으면 재사용한다.
    source_path = Path(asset.storage_path)
    for width in settings.thumbnail_widths:
        if width >= image_info.width:
            continue
        ext = thumbnail_extension(settings.thumbnail_format)
        filename = f"{asset.sha256}__thumb_{width}{ext}"
        output_path = _storage_path(settings.variants_root, asset.sha256, filename)
        if not output_path.exists():
            variant_info = create_thumbnail(source_path, output_path, width, settings.thumbnail_format)
        else:
            # 같은 파생 이미지가 이미 존재하면 메타데이터만 다시 읽는다.
            variant_info = inspect_image(output_path)
        yield VariantRecord(
            kind=f"thumb_{width}",
            format=settings.thumbnail_format,
            width=variant_info.width,
            height=variant_info.height,
            byte_size=output_path.stat().st_size,
            storage_path=str(output_path),
            public_url=_public_url(settings, "variants", asset.sha256, filename),
        )


def delete_files(paths: Iterable[str]) -> None:
    # 삭제 API에서는 DB와 파일시스템 정리를 함께 수행한다.
    for raw_path in paths:
        path = Path(raw_path)
        if path.exists():
            path.unlink()
