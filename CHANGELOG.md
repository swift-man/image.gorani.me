# Changelog

이 프로젝트의 주요 변경 사항을 기록합니다.

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 참고하고, 버전 번호는 [Semantic Versioning](https://semver.org/lang/ko/) 흐름을 따릅니다.

## [0.1.0] - 2026-07-04

### Added

- 이미지 업로드 전용 서비스 초기 구현을 추가했습니다.
- `POST /upload` multipart 이미지 업로드 API를 추가했습니다.
- `GET /healthz` 헬스체크 API를 추가했습니다.
- `GET /assets/<sha256>` 이미지 메타데이터 조회 API를 추가했습니다.
- `DELETE /assets/<sha256>` 원본 및 썸네일 삭제 API를 추가했습니다.
- `X-API-Key` 및 `Authorization: Bearer ...` 기반 쓰기 API 인증을 추가했습니다.
- 프로젝트 루트 `.env` 자동 로딩과 셸 환경변수 우선 적용을 추가했습니다.
- SHA-256 해시 기반 원본 이미지 저장 경로를 추가했습니다.
- 공유폴더 저장소 구조를 추가했습니다.
- 썸네일 생성 기능을 추가했습니다.
- Pillow 기반 WebP 썸네일 생성 지원을 추가했습니다.
- PostgreSQL 메타데이터 스키마를 추가했습니다.
- 서버 시작 시 DB 스키마 자동 적용 기능을 추가했습니다.
- 저장 경로 준비 스크립트 `scripts/prepare-storage.sh`를 추가했습니다.
- 서비스 실행 스크립트 `scripts/run-service.sh`를 추가했습니다.
- 저장소 경로 보정 스크립트 `scripts/fix-storage-root.sh`를 추가했습니다.
- Nginx 정적 딜리버리 연동 문서를 추가했습니다.
- 프론트엔드 이미지 업로드 연동 문서를 추가했습니다.
- 한글 README와 운영 문서를 추가했습니다.

### Fixed

- 실행 스크립트의 기본값이 `.env` 설정을 덮어쓰던 문제를 수정하고 설정 기본값을 애플리케이션으로 일원화했습니다.

### Notes

- 실제 이미지 딜리버리는 이 서비스가 아니라 별도 Nginx 서버가 담당합니다.
- 실제 API 키, DB 비밀번호, `.env` 파일은 공개 저장소에 커밋하지 않습니다.
