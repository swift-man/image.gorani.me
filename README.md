# image.gorani.me

`image.gorani.me`의 업로드 전용 서비스입니다.

이 프로젝트는 다음 역할을 담당합니다.
- 이미지 업로드 받기
- 원본 이미지를 공유폴더에 저장하기
- 선택적으로 썸네일 만들기
- PostgreSQL에 메타데이터 저장하기
- 삭제 요청 시 원본과 썸네일 정리하기

이 프로젝트는 다음 역할을 하지 않습니다.
- 공개 이미지 직접 서빙
- 요청 시점 실시간 리사이즈
- CDN 또는 엣지 캐시 제어

실제 이미지 딜리버리는 별도 Windows 11 + Nginx 서버가 담당합니다.

## 동작 구조

구성은 아래처럼 나뉩니다.

- 업로드 서비스: 업로드, 삭제, 썸네일 생성, 메타데이터 저장
- 공유폴더: 실제 이미지 파일 저장소
- PostgreSQL: 원본/썸네일 메타데이터 저장
- Nginx: 공유폴더의 파일을 정적으로 바로 서빙

즉, 업로드 서비스는 "쓰기"를 담당하고, Nginx는 "읽기와 캐시"를 담당합니다.

### 구성 요소별 역할

조금 더 구체적으로 보면 각 구성 요소는 아래 역할을 맡습니다.

- 업로드 서비스
  업로드 요청을 받고 이미지 유효성을 검사합니다. 원본 파일을 공유폴더에 저장하고, 설정된 크기만큼 썸네일을 만든 뒤 PostgreSQL에 메타데이터를 기록합니다.
- 공유폴더
  원본 이미지와 썸네일 파일이 실제로 저장되는 장소입니다. 업로드 서비스는 여기에 파일을 쓰고, Nginx는 같은 파일을 읽어 사용자에게 전달합니다.
- PostgreSQL
  이미지 자체를 저장하지 않고, 해시값, 파일 경로, 크기, 상태, 썸네일 정보 같은 메타데이터만 관리합니다.
- Nginx
  업로드 서비스를 거치지 않고 공유폴더의 이미지를 직접 읽어 빠르게 응답합니다. 브라우저 캐시와 장기 캐시 헤더도 여기에서 관리합니다.

### 업로드 요청 흐름

이미지 1장을 업로드하면 흐름은 대략 아래 순서로 진행됩니다.

1. 클라이언트가 `POST /upload`로 이미지 파일을 보냅니다.
2. 업로드 서비스가 파일을 임시 파일로 먼저 받습니다.
3. MIME 타입과 이미지 크기를 검사하고 SHA-256 해시를 계산합니다.
4. 해시 기반 경로로 원본 파일을 공유폴더에 저장합니다.
5. 썸네일 옵션이 켜져 있으면 작은 크기 이미지들을 생성해 같은 공유폴더에 저장합니다.
6. 원본과 썸네일 경로, 크기, 상태를 PostgreSQL에 저장합니다.
7. 응답으로 원본 URL과 썸네일 URL 정보를 반환합니다.

즉, 최종 사용자는 나중에 Nginx URL로 이미지를 보게 되고, 업로드 서비스는 업로드 시점에만 개입합니다.

### 삭제 요청 흐름

삭제 요청은 아래 순서로 동작합니다.

1. 클라이언트가 `DELETE /assets/<sha256>`를 호출합니다.
2. 업로드 서비스가 PostgreSQL에서 원본/썸네일 경로를 조회합니다.
3. 썸네일 파일을 먼저 제거합니다.
4. 원본 파일을 제거합니다.
5. DB 상태를 `deleted`로 바꾸고 삭제 시각을 기록합니다.

이 구조 덕분에 Nginx는 파일 생명주기를 직접 관리하지 않아도 됩니다.

## 현재 기준 운영 전제

현재 이 저장소는 아래 환경을 기준으로 맞춰져 있습니다.

- 업로드 실행 환경: macOS
- 공유폴더: `\\DESKTOP-0217PLD\gorani-images`
- macOS 마운트 예시: `/Users/m4_26/mnt/gorani-images`
- 실제 저장 루트 예시: `/Users/m4_26/mnt/gorani-images/image-store`
- PostgreSQL: 로컬 설치 사용
- 썸네일 기본 포맷: `jpeg`

중요한 점:
- 이 macOS 환경의 `sips`는 `webp` 출력이 안정적으로 되지 않아 썸네일 기본값을 `jpeg`로 두었습니다.
- 원본은 해시 기반 경로에 저장됩니다.
- 파일명은 사용자 업로드 이름이 아니라 내부 해시 기반 이름을 사용합니다.

## 요구 사항

실행 전에 아래 항목이 준비되어 있어야 합니다.

- `python3`
- `psql`
- macOS 기본 `sips`
- macOS 기본 `file`
- 접근 가능한 PostgreSQL 서버
- 쓰기 가능한 SMB 공유폴더

현재 구현은 별도 Python 패키지 설치 없이 동작합니다.

## 설치

### 1. 저장소 받기

```bash
git clone https://github.com/swift-man/image.gorani.me.git
cd image.gorani.me
```

### 2. 공유폴더 마운트

macOS에서 SMB 공유폴더를 먼저 마운트해야 합니다.

```bash
mkdir -p ~/mnt/gorani-images
mount_smbfs //ksj@DESKTOP-0217PLD/gorani-images ~/mnt/gorani-images
```

마운트 후 아래가 되어야 합니다.

```bash
ls -la ~/mnt/gorani-images
touch ~/mnt/gorani-images/.write-test && rm ~/mnt/gorani-images/.write-test
```

위 `touch`가 실패하면 업로드 서비스도 공유폴더에 저장할 수 없습니다.

### 3. 저장 디렉터리 준비

서비스는 아래 두 디렉터리를 사용합니다.

- `image-store/original`
- `image-store/variants`

자동 준비 스크립트:

```bash
IMAGE_STORAGE_ROOT=/Users/m4_26/mnt/gorani-images/image-store ./scripts/prepare-storage.sh
```

직접 만들어도 됩니다.

```bash
mkdir -p /Users/m4_26/mnt/gorani-images/image-store/original
mkdir -p /Users/m4_26/mnt/gorani-images/image-store/variants
```

### 4. PostgreSQL 준비

앱 시작 시 [sql/schema.sql](sql/schema.sql)가 자동 적용됩니다.

즉, 최소한 아래 조건만 만족하면 됩니다.

- PostgreSQL 서버가 실행 중일 것
- 현재 사용자 또는 지정 계정이 접속 가능할 것
- `PGDATABASE` 또는 `DATABASE_URL`이 올바를 것

예시:

```bash
export PGDATABASE=postgres
```

### 5. 환경변수 준비

예시 파일은 [.env.example](.env.example)에 있습니다.

중요 환경변수:

- `IMAGE_STORAGE_ROOT`: 실제 공유폴더 저장 루트
- `IMAGE_PUBLIC_PREFIX`: Nginx가 사용할 공개 URL prefix
- `IMAGE_MAX_UPLOAD_BYTES`: 최대 업로드 크기
- `IMAGE_ENABLE_THUMBNAILS`: 썸네일 생성 여부
- `IMAGE_THUMBNAIL_WIDTHS`: 생성할 썸네일 폭 목록
- `IMAGE_THUMBNAIL_FORMAT`: `jpeg`, `png`, `webp`
- `IMAGE_API_KEYS`: 업로드/삭제에 허용할 API 키 목록
- `PGDATABASE` 또는 `DATABASE_URL`: PostgreSQL 접속 대상

공개 저장소 주의:
- 실제 API 키, 비밀번호, 연결 문자열은 커밋하면 안 됩니다.
- `.env` 파일은 `.gitignore`에 포함되어 있습니다.
- `.env.example`에는 예시 값만 넣어두고, 실제 값은 로컬에서만 관리하세요.

## 실행 방법

### 가장 간단한 실행

```bash
cd /Users/m4_26/image.gorani.me

IMAGE_STORAGE_ROOT=/Users/m4_26/mnt/gorani-images/image-store \
IMAGE_THUMBNAIL_FORMAT=jpeg \
IMAGE_API_KEYS='replace-me-with-a-real-secret' \
PGDATABASE=postgres \
./scripts/run-service.sh
```

정상 실행되면 아래처럼 출력됩니다.

```text
Listening on http://127.0.0.1:8080
```

### 기본 포트 변경

```bash
IMAGE_UPLOAD_PORT=8090 \
IMAGE_STORAGE_ROOT=/Users/m4_26/mnt/gorani-images/image-store \
IMAGE_API_KEYS='replace-me-with-a-real-secret' \
PGDATABASE=postgres \
./scripts/run-service.sh
```

## API 사용 방법

### 1. 헬스체크

```bash
curl http://127.0.0.1:8080/healthz
```

예상 응답:

```json
{"status": "ok"}
```

### 2. 업로드

업로드는 `multipart/form-data`로 `image` 필드를 보내야 합니다.

```bash
curl -X POST \
  -H "X-API-Key: replace-me-with-a-real-secret" \
  -F "image=@/path/to/photo.png" \
  http://127.0.0.1:8080/upload
```

예상 응답 예시:

```json
{
  "id": 1,
  "sha256": "....",
  "original": {
    "url": "/i/original/ab/cd/<sha256>.png",
    "content_type": "image/png",
    "width": 400,
    "height": 400,
    "bytes": 4889
  },
  "variants": [
    {
      "kind": "thumb_160",
      "url": "/i/variants/ab/cd/<sha256>__thumb_160.jpg",
      "width": 160,
      "height": 160,
      "bytes": 5364
    }
  ]
}
```

### 3. 메타데이터 조회

```bash
curl http://127.0.0.1:8080/assets/<sha256>
```

조회는 현재 인증 없이 가능합니다.

### 4. 삭제

```bash
curl -X DELETE \
  -H "X-API-Key: replace-me-with-a-real-secret" \
  http://127.0.0.1:8080/assets/<sha256>
```

예상 응답:

```json
{"status": "deleted", "sha256": "<sha256>"}
```

## 저장 구조

실제 파일은 해시 기반 경로에 저장됩니다.

예시:

- 원본: `original/43/b7/<sha256>.png`
- 썸네일: `variants/43/b7/<sha256>__thumb_160.jpg`

이 구조의 장점:

- 디렉터리 편중 방지
- 동일 파일 중복 관리 용이
- 캐시 가능한 불변 URL 구성 쉬움

### 디렉터리 구조 예시

저장 루트 아래 구조는 대략 아래처럼 됩니다.

```text
image-store/
├── original/
│   └── 43/
│       └── b7/
│           └── 43b78bfe96a88f7f2b500ee07b389d9838d8c10b698d96223513b865415b967a.png
└── variants/
    └── 43/
        └── b7/
            ├── 43b78bfe96a88f7f2b500ee07b389d9838d8c10b698d96223513b865415b967a__thumb_160.jpg
            └── 43b78bfe96a88f7f2b500ee07b389d9838d8c10b698d96223513b865415b967a__thumb_320.jpg
```

첫 두 단계 폴더인 `43/b7` 같은 값은 SHA-256 앞부분을 잘라서 만든 것입니다. 이렇게 하면 한 디렉터리에 파일이 과도하게 몰리는 것을 줄일 수 있습니다.

### DB와 파일의 관계

파일은 공유폴더에 있고, DB에는 그 파일을 가리키는 정보만 있습니다.

- `assets`
  원본 이미지 1건을 나타냅니다.
- `asset_variants`
  원본 이미지에 연결된 썸네일들을 나타냅니다.

즉, 원본 1개와 썸네일 여러 개를 DB에서 연결해 관리하는 구조입니다.

## 캐시

캐시는 이 업로드 서비스가 아니라 Nginx가 주로 담당합니다.

권장 방식:

- 해시 기반 불변 URL 사용
- Nginx에서 긴 `Cache-Control` 부여
- 브라우저와 OS 캐시 활용

권장 Nginx 응답 헤더 예시:

```nginx
expires 1y;
add_header Cache-Control "public, max-age=31536000, immutable";
```

## Nginx 연동

Nginx 프로젝트에 전달할 상세 계약은 [docs/nginx-integration.md](docs/nginx-integration.md)에 정리되어 있습니다.

핵심만 요약하면:

- 업로드 서비스는 공유폴더에 저장만 함
- Nginx는 그 파일을 직접 읽어 서빙함
- 앱을 거치지 않고 정적 파일로 바로 응답함
- 공개 URL 규칙은 업로드 서비스와 Nginx가 동일하게 맞춰야 함

## 현재 구현 제약

- 이미지 검사와 썸네일 생성은 macOS `sips` 기반입니다.
- 현재 환경에서는 `webp` 썸네일 출력보다 `jpeg`가 안전합니다.
- 읽기 API는 공개 상태이고, 쓰기 API만 API key로 보호합니다.
- PostgreSQL 연결은 현재 Python 드라이버가 아니라 `psql` CLI를 사용합니다.

## 추천 운영 순서

운영할 때는 아래 순서가 가장 안전합니다.

1. SMB 공유폴더 마운트 확인
2. 공유폴더 쓰기 테스트
3. PostgreSQL 접속 확인
4. API 키 설정
5. 업로드 서비스 실행
6. 테스트 업로드
7. Nginx에서 생성된 파일 URL 확인

## 관련 문서

- [AGENTS.md](AGENTS.md)
- [docs/setup.md](docs/setup.md)
- [docs/nginx-integration.md](docs/nginx-integration.md)
- [sql/schema.sql](sql/schema.sql)

## 앞으로 보강하면 좋은 항목

- `README`의 `.env` 예시 추가
- 서비스 데몬 등록 방법 정리
- 구조화 로그 추가
- 백그라운드 썸네일 작업 분리
- native PostgreSQL 드라이버로 교체
