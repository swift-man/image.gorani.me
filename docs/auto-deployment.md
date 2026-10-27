# main 자동 배포와 로그인 복구

## 적용 범위

이 기능은 **별도 설치 후** 활성화됩니다. PR 생성/병합만으로 현재 운영 LaunchAgent를 바꾸지 않습니다.
이미지 파일과 PostgreSQL은 이동하지 않으며, Nginx의 정적 URL/캐시 계약도 그대로 유지합니다.

```text
공개 저장소 main push -> GitHub-hosted CI 성공 -> 비공개 workflow 알림
                                                 |
                             기존 deploy runner 둘 중 한 대 -> SSH
                                                 |
Mac 로그인 + 5분 주기 -------------------------> 같은 배포기/잠금
                                                 |
main/CI 재확인 -> 새 release/.venv -> 검사 -> current 전환 -> 재시작/검증
                                                 | 실패
                                                 +-> 이전 release 복구
```

공개 PR은 GitHub-hosted macOS에서만 검사합니다. 운영 SSH 키가 있는 self-hosted runner에
외부 PR 코드를 실행시키지 않습니다. runner는 빌드 파일을 보내지 않고 운영 Mac의 고정된
`sync.sh`만 호출합니다. 어느 runner가 작업을 받아도 같은 Mac에 배포되며 서버가 두 개 생기지 않습니다.

**전원 켜짐과 로그인은 다릅니다.** LaunchAgent이므로 해당 macOS 사용자가 로그인해야 실행됩니다.
FileVault 잠금 해제/키체인/SMB 권한을 우회하지 않습니다. Windows가 늦게 켜지면 기존 SMB
supervisor가 재연결하고 updater가 다음 주기에 갱신합니다. 두 runner가 꺼져 있어도 운영 Mac의
로그인/주기 updater는 동작합니다. 네트워크/GitHub/API 제한이 있으면 기존 버전을 유지하고
검증 불가를 드러내기 위해 실패(종료 코드 1)로 기록한 뒤 다음 주기에 다시 검사합니다.
CI가 단순히 아직 성공하지 않은 경우의 `적용 보류`(종료 코드 0)와 구분합니다.

## 1. 운영 Mac 최초 설치

먼저 이 PR을 main에 병합하고, 기존 운영 checkout에서 아래를 실행합니다. 개발 worktree에서
설치하지 마세요. 기존 `.env`, 공유폴더와 PostgreSQL, 네이티브 LaunchAgent가 정상이어야 합니다.
Python 3.11 이상, git, psql, file, sips, zsh가 필요하며 Python 기본 설치 경로를 계속 유지해야 합니다.
기존 3.9/3.10 환경은 [0.4.0 전환 절차](deployment-hardening.md)를 먼저 확인하세요.

```sh
git switch main
git pull --ff-only
chmod 600 .env
./scripts/restart-service.sh
./scripts/service-status.sh
.venv/bin/python scripts/deploy_service.py install
```

설치는 서비스 plist를 백업하고 작업 디렉터리를 `current` 링크로 바꿉니다. 이 과정에 짧은
재시작이 있습니다. `.env`는 기존 checkout에 그대로 두고 release에서 링크하므로 그 파일과
checkout을 삭제하지 마세요. 기존 `.venv`도 건드리지 않습니다. Git에 추적되어 있는 `.venv`는
배포 archive에서 제외하고 커밋마다 새로 만듭니다. 런타임과 빌드 의존성은
`requirements-deploy.txt`의 버전/SHA-256으로 검증하며 wheel이 없는 플랫폼에서는 실패합니다.
프로젝트 빌드는 `--no-build-isolation --no-deps`로 추가 의존성 다운로드를 막습니다.

설치된 안정 버전 배포기(`manager.py`)는 main으로 자동 덮어쓰지 않습니다. 배포기 자체 또는
`sql/schema.sql`, 네이티브 Swift 런처, SSH 강제 명령 진입점을 변경할 때는 아래의 수동 업그레이드 절차를 사용합니다.
스키마/런처/배포기 변경은 자동 배포가 거부합니다. 일반 Python 코드에 숨겨진 DB 마이그레이션까지
감지하는 장치는 아니므로 리뷰에서 데이터 호환성을 별도로 확인해야 합니다.

## 2. runner 연결

비공개 `gorani-inc/self-host-runner`의 `image-upload-deploy.yml`을 main에 병합합니다.

| 저장소 | 종류 | 이름 | 내용 |
| --- | --- | --- | --- |
| 비공개 runner 저장소 | Secret | `IMAGE_DEPLOY_HOST_MAC` | 실제 운영 Mac 내부 호스트/IP |
| 비공개 runner 저장소 | Secret | `IMAGE_DEPLOY_USER_MAC` | LaunchAgent를 실행하는 macOS 사용자 |
| 비공개 runner 저장소 | Variable | `IMAGE_DEPLOY_SSH_PORT` | 생략 시 22 |
| 비공개 runner 저장소 | Variable | `IMAGE_DEPLOY_ENABLED` | 준비 완료 후 `true` |
| 공개 이미지 저장소 | Secret | `IMAGE_DEPLOY_DISPATCH_TOKEN` | 아래 최소권한 토큰 |

두 runner 각각의 `~/.ssh/ci_deploy` 공개키를 운영 사용자에게 등록하고, 신뢰할 수 있는 경로로
확인한 운영 Mac SSH 호스트키를 각 runner의 `known_hosts`에 등록합니다. workflow는
`StrictHostKeyChecking=yes`를 사용하며 임의 `ssh-keyscan` 결과를 자동 신뢰하지 않습니다.
기존 API 프로젝트와 같은 키를 재사용할 수 있지만 전용 키와 강제 명령/포워딩 금지를 권장합니다.
SSH 접속은 사설망/VPN으로 한정하고 인터넷에 22/5432/445/8080 포트를 열지 않습니다.

dispatch 토큰은 **비공개 runner 저장소 한 개의 Actions: write**만 허용하는 fine-grained PAT 또는
GitHub App installation token이어야 합니다. 만료/갱신을 관리하세요. 일반 `GITHUB_TOKEN`은 다른
저장소에 접근할 수 없습니다. 토큰 미등록 시 즉시 알림만 건너뛰고 로그인/5분 갱신은 계속됩니다.
비공개 workflow는 활성화 후 수동 및 15분 간격 복구 실행도 지원합니다.

Actions: write는 이 배포 workflow 하나로 한정된 권한이 아닙니다. 같은 저장소의 다른 수동 workflow
실행과 Actions 관리에도 영향을 줄 수 있으므로 토큰 유출의 영향 범위를 저장소 전체로 평가하세요.
공개 저장소 main/workflow 변경 권한자를 신뢰해야 하며, 활성화 전 다른 deploy workflow의 ref 제한도
점검합니다. main 전용 Environment 사용을 검토하되 workflow의 environment 지정과 보호 규칙을
함께 구성해야 합니다. 현재 workflow에는 Environment 보호가 적용되어 있지 않습니다.

GitHub 설정/키는 파일이나 PR 본문에 쓰지 않습니다. 이 변경에는 실제 secret 등록이나
runner 재설정이 포함되지 않습니다.

전용 배포 키에는 [SSH 강제 명령 설정](deployment-hardening.md)을 적용할 수 있습니다.
설치기는 `ssh-entry.sh`만 준비하며 authorized_keys를 자동 수정하지 않습니다.

## 성공 확인과 로그

```sh
"$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"
tail -n 80 "$HOME/Library/Application Support/gorani-image-upload/deploy/deploy.log"
tail -n 80 "$HOME/Library/Application Support/gorani-image-upload/deploy/deploy-error.log"
curl -i http://127.0.0.1:8080/healthz
./scripts/service-status.sh
```

포트나 수신 주소를 바꿨다면 curl 주소도 변경합니다. updater는 설치 당시 서비스 plist의
`IMAGE_UPLOAD_HOST`와 포트를 사용하며 wildcard 주소만 로컬 검사 주소로 바꿉니다.
필수 plist 키나 PATH가 빠지면 서비스를 변경하기 전에 누락된 키를 안내하고 설치를 거부합니다.
수동/runner 실행 로그는 해당 터미널/Actions에 출력되고 주기 실행 로그는 위 파일에 기록됩니다.

사전 검사 실패는 CONFIG, SMB, STORAGE, DATABASE, PROCESS, TIMEOUT 코드로 구분합니다.
자식 프로세스 출력과 예외 원문은 비밀값 보호를 위해 출력하지 않습니다. 코드는 원인 영역이며
권한/경로/네트워크의 세부 원인을 확정하는 진단은 아닙니다.

- `배포 완료: <sha>`: 새 release의 SHA와 같은 PID가 5초 이상 정상 응답하고 SMB/DB 재검사도 성공.
- `최신 release 정상: <sha>`: 이미 최신이고 추가 재시작은 하지 않음.
- `적용 보류`: 아직 최신 main CI가 성공하지 않았음. **배포 성공이라는 뜻이 아님.**
- `자동 배포 실패`: 기존 버전 유지 또는 journal 기반 복구. 전환 전 준비 실패는 다음 주기에 재시도하지만,
  활성화 실패 커밋은 아래 `retry` 명령으로 차단을 해제하거나 새 main 커밋이 생기기 전까지 재시도하지 않음.

`/healthz`의 JSON은 그대로 `{"status":"ok"}`입니다. `X-Image-Release`, `X-Image-Pid`,
`Cache-Control: no-store`가 추가되며 관리 배포 밖에서는 release가 `unmanaged`입니다.
이 API 자체가 DB/SMB readiness를 검사하는 것은 아닙니다. 배포기는 별도로 DB `SELECT 1`,
정확한 SMB 대상, 원본/variant 저장 경로 쓰기 준비를 검사합니다. 공개 이미지 업로드/다운로드의
전체 E2E 성공을 의미하지는 않습니다.

## 실패, 보존과 수동 복구

배포는 `flock`으로 직렬화합니다. 전환 직전 `pending.json`에 이전 경로를 남기므로 updater가
강제 종료되면 다음 실행에서 이전 release로 복구합니다. 앱 전환만 되돌리며 DB/이미지 내용을
롤백하지 않습니다. 단일 프로세스 재시작이므로 짧은 업로드 중단과 진행 중 요청 실패 가능성이
있습니다. 클라이언트는 재시도하며 내용 해시 중복 방지를 이용하세요.

SIGTERM은 일반 연결 실패로 재시도하지 않습니다. 진행 중 명령의 자식 프로세스를 회수하고,
전환 중이었다면 복구를 시도한 뒤 중단 상태(종료 코드 143)를 반환합니다. 강제 종료까지 겹쳐
복구가 끝나지 못한 경우에는 journal을 다음 실행에 보존합니다.

활성화에 실패하거나 전환 중 강제 종료된 커밋은 `failed.json`으로 차단합니다. 같은 커밋을
5분마다 재시작해 장애를 반복하지 않으며, 새 main 커밋은 다시 검사합니다. 원인을 수정한 뒤
같은 커밋을 명시적으로 재시도하려면 다음 명령으로 차단만 해제합니다(즉시 재시작하지 않음).

`pending.json`이 남아 있으면 `retry`는 거부됩니다. 먼저 위의 `sync.sh`를 실행해 이전 release를
복구하세요. 복구가 성공해도 같은 main 커밋의 `failed.json` 차단 때문에 `sync`가 종료 코드 `1`을
반환할 수 있습니다. 종료 코드만 보지 말고 로그와 서비스 상태를 확인하고, `pending.json`이
사라진 뒤에만 아래 명령을 실행하세요. 파일이 남아 있으면 삭제하거나 차단을 우회하지 말고 복구 실패 원인을 확인합니다.

```sh
test ! -e "$HOME/Library/Application Support/gorani-image-upload/deploy/pending.json" && \
.venv/bin/python "$HOME/Library/Application Support/gorani-image-upload/deploy/manager.py" retry
```

전체 시험 모음은 GitHub-hosted CI에서 실행하며, DB 통합 시험은 `initdb`·`pg_ctl`·`psql`이
모두 있을 때만 수행하고 없으면 건너뜁니다. 운영 Mac에서 임시 DB daemon을 만들지 않도록
배포 준비는 독립 가상환경 설치, `pip check`, 구문 검사와 실제 readiness만 수행합니다.

성공한 과거 release와 로그는 자동 삭제하지 않습니다. 디스크 여유가 2 GiB 미만이면 새 준비를
중단합니다. 정리할 때는 `current` 대상과 `pending.json`의 복구 대상을 보존하세요. 운영 이미지
공유폴더는 release 정리 대상이 아닙니다.

자동 배포를 중지할 때에는 **먼저 비공개 저장소 `IMAGE_DEPLOY_ENABLED=false`**로 설정하고
실행 중인 runner 작업이 끝났는지 확인합니다. 그 뒤:

```sh
launchctl bootout "gui/$(id -u)/me.gorani.image-upload.deploy"
```

이것만으로 수동 SSH 호출을 차단하지는 않습니다. 필요하면 배포 키 접근도 중지합니다.
서비스 자체는 마지막 성공 release에서 계속 실행됩니다. 다음 로그인에도 updater를 끄려면
`~/Library/LaunchAgents/me.gorani.image-upload.deploy.plist`를 LaunchAgents 밖으로 옮기세요.

자동 배포 제거/수동 업그레이드는 updater와 모든 runner가 멈춘 상태에서 합니다.
`deploy/original-service.plist`를 기존 `~/Library/LaunchAgents/me.gorani.image-upload.plist`로
복원하고 서비스 `bootout`/`bootstrap` 후 상태를 확인하세요. 기존 checkout을 원하는 main으로
갱신하고 스키마/네이티브 앱 변경을 검토·백업한 뒤 필요하면 `install-launch-agent.sh`를 실행합니다.
기존 deploy 디렉터리는 삭제 대신 백업 이름으로 이동하고 새 `deploy_service.py install`로 설치합니다.
설치 실패 시 plist는 복원되지만 진단 자료 디렉터리는 남으므로 상태 확인 전 무조건 재설치하지 마세요.

## 근거

- [GitHub self-hosted runner 보안 주의](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
- [workflow dispatch 최소 권한](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
- [runner 배정과 오프라인 큐 제한](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
