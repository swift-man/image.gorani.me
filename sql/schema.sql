-- 첫 문장에서 search_path 를 pg_catalog, pg_temp 로 고정한다(plain SET). public 에 CREATE 권한만 있는 역할이
-- 심은 함수·연산자가 이 스키마 적용을 가로채지 못하게 한다. 고정한 경로에는 public 이 없어 기본 생성 스키마가
-- pg_catalog 이므로, 아래 객체는 모두 public. 으로 만든다.
SET search_path = pg_catalog, pg_temp;

-- 원본 이미지 메타데이터를 저장하는 메인 테이블이다.
-- 실제 바이너리는 공유폴더에 있고, DB에는 경로와 상태만 둔다.
CREATE TABLE IF NOT EXISTS public.assets (
    id BIGSERIAL PRIMARY KEY,
    sha256 CHAR(64) NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    file_ext TEXT NOT NULL,
    byte_size BIGINT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    storage_path TEXT NOT NULL UNIQUE,
    public_url TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_assets_status ON public.assets (status);
CREATE INDEX IF NOT EXISTS idx_assets_created_at ON public.assets (created_at DESC);

-- 썸네일 등 파생 이미지는 원본과 1:N 관계로 별도 관리한다.
CREATE TABLE IF NOT EXISTS public.asset_variants (
    id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL REFERENCES public.assets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    format TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    byte_size BIGINT NOT NULL,
    storage_path TEXT NOT NULL UNIQUE,
    public_url TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (asset_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_asset_variants_asset_id ON public.asset_variants (asset_id);

-- 포맷 교체 등으로 더 이상 참조되지 않는 파일을 안전하게 재시도 삭제하기 위한 큐다.
CREATE TABLE IF NOT EXISTS public.pending_file_deletions (
    id BIGSERIAL PRIMARY KEY,
    storage_path TEXT NOT NULL UNIQUE,
    asset_sha256 CHAR(64),
    reason TEXT NOT NULL,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

ALTER TABLE public.pending_file_deletions
ADD COLUMN IF NOT EXISTS asset_sha256 CHAR(64);

CREATE INDEX IF NOT EXISTS idx_pending_file_deletions_open
ON public.pending_file_deletions (id)
WHERE completed_at IS NULL;
