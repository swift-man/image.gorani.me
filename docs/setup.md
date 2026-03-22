# Local Setup

## What Exists

This repository now contains a minimal upload-side service that:
- accepts image uploads at `POST /upload`
- stores originals in a shared folder
- optionally generates thumbnail variants
- records metadata in PostgreSQL through `psql`
- deletes managed files with `DELETE /assets/<sha256>`
- exposes metadata at `GET /assets/<sha256>`

## Endpoints

### `POST /upload`

Multipart form upload with field name `image`.

Example:

```bash
curl -X POST \
  -H "X-API-Key: replace-me" \
  -F "image=@/path/to/photo.jpg" \
  http://127.0.0.1:8080/upload
```

### `GET /assets/<sha256>`

Returns stored metadata and known variants.

### `DELETE /assets/<sha256>`

Deletes managed variant files and the original file, then marks the asset deleted in PostgreSQL.

Example:

```bash
curl -X DELETE \
  -H "X-API-Key: replace-me" \
  http://127.0.0.1:8080/assets/<sha256>
```

## Environment Variables

See [.env.example](/Users/m4_26/image.gorani.me/.env.example).

Important values:
- `IMAGE_STORAGE_ROOT`: mounted shared folder path
- `IMAGE_PUBLIC_PREFIX`: public URL base expected by Nginx
- `IMAGE_ENABLE_THUMBNAILS`: `1` or `0`
- `IMAGE_THUMBNAIL_WIDTHS`: comma-separated widths
- `IMAGE_THUMBNAIL_FORMAT`: `jpeg`, `png`, or `webp`
- `IMAGE_API_KEYS`: comma-separated API keys accepted for upload and delete
- `PGDATABASE` or `DATABASE_URL`: PostgreSQL target

Current shared folder:
- Windows share: `\\\\DESKTOP-0217PLD\\gorani-images`
- On macOS, mount it first and point `IMAGE_STORAGE_ROOT` at the mounted path
- Working example mount path for this machine: `/Users/m4_26/mnt/gorani-images/image-store`

Example mount command on macOS:

```bash
mkdir -p ~/mnt/gorani-images
mount_smbfs //USERNAME@DESKTOP-0217PLD/gorani-images ~/mnt/gorani-images
```

Prepare storage directories:

```bash
IMAGE_STORAGE_ROOT=/Users/m4_26/mnt/gorani-images/image-store ./scripts/prepare-storage.sh
```

## Run

```bash
IMAGE_STORAGE_ROOT=/Users/m4_26/mnt/gorani-images/image-store \
IMAGE_THUMBNAIL_FORMAT=jpeg \
IMAGE_API_KEYS='replace-me' \
./scripts/run-service.sh
```

## Database

The app auto-applies [schema.sql](/Users/m4_26/image.gorani.me/sql/schema.sql) on startup.

It currently talks to PostgreSQL via the `psql` CLI, which keeps the project dependency-light for now.

## Current Constraints

- image inspection and thumbnail generation depend on macOS `sips`
- supported input formats depend on what `sips` and `file` can read
- this machine cannot write WebP thumbnails with `sips`, so `jpeg` is the safe default thumbnail format here
- this version assumes the shared folder is already mounted before startup
- deletes are hard file deletes plus metadata soft-delete

## Recommended Next Steps

- add structured logging
- add background jobs for thumbnail generation if upload latency becomes a concern
- replace `psql` subprocess access with a native PostgreSQL client when the runtime stack is finalized
