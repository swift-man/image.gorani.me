# image.gorani.me Upload Service

This repository is the upload-side service for `image.gorani.me`.

It is responsible for:
- accepting image uploads
- validating image files and metadata
- storing original files in a shared folder
- optionally generating thumbnails or other derived variants
- deleting assets and their managed variants
- storing metadata in PostgreSQL
- enforcing API key authentication for write operations

It is not responsible for:
- public image delivery
- image resizing on request
- edge caching or CDN behavior
- runtime image transformation for end users

## System Role

The delivery path is handled by an Nginx server running on a separate Windows 11 machine.

The upload service writes image files into a shared folder that is also accessible to the Nginx server. Nginx serves those files directly as static assets from NTFS storage.

Known shared folder:
- Windows share: `\\\\DESKTOP-0217PLD\\gorani-images`
- macOS mount target should typically look like `/Volumes/gorani-images`

This means:
- this project owns writes, deletes, metadata, and variant generation
- Nginx owns public delivery and cache headers
- PostgreSQL owns metadata, variant relationships, and lifecycle state

## Storage Model

Use shared-folder storage as the source of truth for image binaries.

Recommended layout:
- `original/<aa>/<bb>/<sha256>.<ext>`
- `variants/<aa>/<bb>/<sha256>__thumb_<width>.webp`

Rules:
- never use the user-provided filename as the internal storage filename
- prefer hash-based immutable paths
- write to a temporary file first, then atomically rename into place
- avoid overwriting existing files
- treat generated variants as managed children of the original asset

## Database Model

PostgreSQL stores metadata only, not original binary image data.

Expected metadata areas:
- asset identity
- hash and deduplication
- original file path
- content type
- byte size
- image width and height
- creation timestamp
- deletion state
- variant records and their dimensions, format, and path

## Cache Expectations

Caching is primarily owned by the Nginx delivery server and downstream clients.

This service should support that model by:
- generating immutable file paths
- avoiding in-place file replacement
- creating new variant files instead of mutating old ones
- treating deletes carefully because previously issued URLs may remain cached

## Deletion Rules

Deletion should be metadata-aware and storage-aware.

Recommended behavior:
- remove or mark the database record as deleted
- remove managed variants first
- remove the original file last
- handle partial failure safely and log any orphaned files

When cache longevity matters, soft-delete in metadata may be safer than immediate hard deletion.

## Thumbnail Rules

Thumbnail generation is optional but first-class.

Recommended defaults:
- generate only a small, fixed set of sizes
- store variants as separate files
- prefer WebP for derived thumbnails unless compatibility requirements say otherwise
- keep original upload format for originals

Avoid on-demand thumbnail generation in this service unless explicitly designed later.

## Operational Principles

- validate the file contents, not just the extension
- store a strong content hash such as SHA-256
- make upload idempotency possible through hash lookup
- keep file writes and database writes consistent as much as possible
- design for orphan cleanup and retry after partial failure
- keep public delivery concerns out of this codebase unless needed for coordination

## Database Access Rules

- 서비스가 `psql`로 보내는 모든 SQL은 첫 문장을 `SET search_path = pg_catalog, pg_temp;`(plain `SET`, `LOCAL` 아님)로 시작하고, 테이블과 비-`pg_catalog` 객체를 `public.`으로 가리킨다. `public` 스키마에 CREATE 권한을 가진 역할이 심은 형변환 오버로드가, 운영에서 슈퍼유저로 접속하는 이 서비스의 SQL을 가로채 그 권한으로 실행되지 못하게 하기 위함이다. `public`을 `search_path`에서 빼면 그 오버로드는 qualification 없는 호출의 후보가 되지 못한다.
- `psql`은 `-q`로 호출한다. 고정 문장의 `SET` 명령 태그가 `-At` 파싱 대상 출력 앞에 섞이지 않게 하기 위함이다.
- 새 SQL을 추가하면 같은 규칙을 지키고, `tests/test_search_path_pin.py`를 실행한다. 이 파일은 각 메서드의 qualification을 확인하고, 폐기용 PostgreSQL 클러스터에서 `public`에 CREATE만 있는 역할이 심은 오버로드를 고정 없는 대조는 슈퍼유저로 부르고 실제 코드는 하나도 부르지 않음을 증명한다.

## Agent Response Rules

- 코드가 변경되었다면 최종 응답에 재실행 명령어를 함께 안내한다.
- LaunchAgent 설치 환경의 재실행 명령어는 `./scripts/restart-service.sh`를 우선 안내한다.
- LaunchAgent를 설치하지 않은 개발 환경에서는 `./scripts/run-service.sh`를 안내하고, 필요한 환경변수가 있으면 같이 적는다.
- 코드가 아닌 문서만 변경했다면 재실행이 필요 없다고 명확히 말한다.

## PR Review Rules

- PR 리뷰를 수행할 때 라인 코멘트를 남길 수 있는 도구나 권한이 있다면, 가능한 한 PR comment 또는 inline review comment로 남긴다.
- PR comment를 남길 수 없는 환경이라면, 최종 응답에 파일과 라인 기준으로 리뷰 코멘트를 정리한다.

## Coordination With Nginx Project

Anything that affects public URLs, static path layout, variant naming, or cache headers must be documented in `docs/nginx-integration.md` and treated as a contract with the Nginx project.
