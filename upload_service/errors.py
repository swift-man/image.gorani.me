class UploadTooLargeError(ValueError):
    """업로드 이미지 바이트 수가 설정된 제한을 초과했음을 나타낸다."""


class ServiceTimeoutError(RuntimeError):
    """외부 시스템 또는 명령 실행이 운영 제한시간을 초과했음을 나타낸다."""


class ImageProcessingTimeoutError(ServiceTimeoutError):
    """이미지 검사 또는 변환 명령이 제한시간 안에 끝나지 않았음을 나타낸다."""


class DatabaseTimeoutError(ServiceTimeoutError):
    """PostgreSQL 연결 또는 쿼리가 제한시간 안에 끝나지 않았음을 나타낸다."""
