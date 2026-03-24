# Nginx Integration Notes

This document is for the Nginx delivery project that serves files written by the `image.gorani.me` upload service.

## Purpose

The upload service does not deliver public images directly.

Its job is to:
- accept uploads
- store originals into the shared folder
- generate optional thumbnails
- delete managed files
- write metadata into PostgreSQL

The Nginx project is expected to:
- read files directly from the shared folder on the Windows 11 host
- expose stable public URLs
- set cache headers for efficient delivery
- return static files without depending on the upload service for reads

Known shared folder:
- Windows UNC path: `\\\\DESKTOP-0217PLD\\gorani-images`
- macOS uploader should mount it over SMB, typically as `/Volumes/gorani-images`

## Delivery Contract

Nginx should treat the shared folder as the source for static delivery.

Recommended path contract:
- originals: `/i/original/<aa>/<bb>/<sha256>.<ext>`
- variants: `/i/variants/<aa>/<bb>/<sha256>__thumb_<width>.<ext>`

Optional namespaced path contract for security symbols:
- originals: `/i/symbols/original/<aa>/<bb>/<sha256>.<ext>`
- variants: `/i/symbols/variants/<aa>/<bb>/<sha256>__thumb_<width>.<ext>`

Recommended on-disk contract:
- `original/<aa>/<bb>/<sha256>.<ext>`
- `variants/<aa>/<bb>/<sha256>__thumb_<width>.<ext>`

Optional namespaced on-disk contract for security symbols:
- `symbols/original/<aa>/<bb>/<sha256>.<ext>`
- `symbols/variants/<aa>/<bb>/<sha256>__thumb_<width>.<ext>`

Recommended absolute storage root examples:
- Windows/Nginx host: `C:\\path\\to\\gorani-images\\image-store` or the actual local NTFS directory behind the share
- macOS uploader: `/Volumes/gorani-images/image-store`

`<aa>` and `<bb>` are the first two path segments derived from the SHA-256 hash to avoid oversized directories.

## Cache Ownership

Cache behavior is owned by Nginx and clients, not by the upload application.

Recommended response headers for immutable image URLs:

```nginx
expires 1y;
add_header Cache-Control "public, max-age=31536000, immutable";
```

Why this works:
- file names are hash-based and immutable
- changed content should produce a new file path
- Nginx can safely serve aggressive cache headers

## Important Assumptions

The upload project assumes:
- Nginx serves files directly from disk
- files are not modified in place after being published
- public URLs map predictably to the shared storage layout
- Nginx does not need to call the upload app to serve an image

## Write Safety

The upload service is expected to write files using:
- temporary file write
- final atomic rename into target path

The Nginx project should assume:
- a file only becomes publicly readable after the final rename
- partially written files should never appear under the final public path

## Deletion Semantics

Deletes are initiated by the upload service.

Nginx should not try to manage file lifecycle.

Important note:
- previously served files may remain in browser cache because delivery URLs are expected to be aggressively cacheable
- if strict invalidation is needed, it must be handled at the URL/versioning level rather than by relying on immediate cache purge

## Thumbnail Contract

If thumbnail generation is enabled, Nginx should serve them as plain static files the same way as originals.

Example variant names:
- `<sha256>__thumb_160.webp`
- `<sha256>__thumb_320.webp`
- `<sha256>__thumb_640.webp`

The exact variant list and output format should be treated as a shared contract between the upload project and the Nginx project.

## Windows 11 Host Notes

Because Nginx runs on Windows 11 and storage is on NTFS:
- the upload project should write into a shared folder mounted from the Windows host
- Nginx should read that same storage locally on the Windows machine
- internal storage names should stay ASCII-safe and hash-based
- avoid depending on user-uploaded filenames for public file paths

## Questions The Nginx Project Should Confirm

- Which local NTFS directory is the document root for image delivery?
- What public URL prefix will be used for originals and variants?
- Will Nginx expose directory listing or deny it?
- Will non-existent files return plain `404` without app fallback?
- Are there any size limits or MIME restrictions to mirror at the edge?
- Is a CDN planned in front of Nginx later?
