from .server import serve


def main() -> None:
    # 엔트리포인트는 서버 실행만 담당한다.
    serve()


if __name__ == "__main__":
    main()
