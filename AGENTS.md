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

## Coordination With Nginx Project

Anything that affects public URLs, static path layout, variant naming, or cache headers must be documented in `docs/nginx-integration.md` and treated as a contract with the Nginx project.
