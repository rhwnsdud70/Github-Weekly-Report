# GitHub 주간 AI·개발 이슈 자동화

매주 GitHub의 최근 변화와 공개 근거를 모아 서로 다른 프로젝트 TOP 5를 선정하고, 상세 한국어 리포트를 Telegram으로 전송하는 Python 3.11+ / SQLite 배치 시스템입니다.

최종 기준은 [마스터 사양서](docs/GitHub_주간_AI_개발_이슈_자동화_마스터_사양서_v1.0.txt)입니다. 개발 재개 시 [AGENTS.md](AGENTS.md), 마스터 사양서, [구현 상태](docs/IMPLEMENTATION_STATUS.md)를 차례로 읽으세요. 실제 검증 결과는 [검증 기록](docs/VALIDATION_REPORT.md)에 있습니다.

## 설치

Python 3.11 이상을 설치하고 저장소 루트에서 실행합니다.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

이하 `python`은 가상환경의 Python입니다. 활성화 없이 `.venv/Scripts/python.exe` 또는 `.venv/bin/python`으로 바꿔 실행할 수 있습니다. PowerShell 활성화는 `.venv/Scripts/Activate.ps1`, Bash는 `source .venv/bin/activate`입니다.

개발에 사용한 이 PC에서는 기본 `python` shim과 `py` 런처가 설정되지 않았습니다. 다음 기존 실행 파일로 테스트와 Dry Run을 검증했습니다.

```powershell
& 'C:/Users/고준영/.pyenv/pyenv-win/versions/3.11.9/python.exe' -m pytest -q
```

빈 가상환경 생성은 성공했지만, 해당 실행 환경의 패키지 인덱스 접근 제한으로 새 의존성 다운로드는 검증하지 못했습니다. 패키지 인덱스에 접근할 수 있는 환경에서 위 설치 명령을 실행하세요. 기본 KST 처리는 표준 라이브러리의 UTC+9 고정 오프셋을 사용합니다. `tzdata`는 다른 IANA 시간대의 Windows 설정 검증을 지원합니다.

## 설정과 Secret

`.env.example`에는 실제 Secret이 없습니다. `.env`는 Git에서 제외됩니다. 프로세스 환경 변수가 `.env`보다 우선합니다.

| 변수 | 용도 / 기본값 |
|---|---|
| `GITHUB_TOKEN` | GitHub 공개 Repository 및 GraphQL 조회용 토큰 |
| `OPENAI_API_KEY` | OpenAI API 프로젝트 키 |
| `OPENAI_MODEL` | 구조화 출력 모델 ID, 기본 `gpt-4.1-mini`; 계정에서 사용 가능한 호환 모델로 변경 가능 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot 토큰 |
| `TELEGRAM_CHAT_ID` | 개인/그룹/채널 발송 목적지 |
| `DATABASE_PATH` | 운영 SQLite 경로, 기본 `data/github_weekly_report.db` |
| `TIMEZONE` | 설정 시간대, 기본 `Asia/Seoul`; 주간 조사/발송 경계는 사양대로 항상 KST |

GitHub에서는 공개 저장소 읽기에 필요한 범위만 가진 토큰을 발급해 `GITHUB_TOKEN`에 설정합니다. Discussions 조회 권한 또는 저장소 설정에 따라 해당 자료가 제공되지 않을 수 있으며, 이 경우 Unknown으로 남깁니다. 프로젝트는 저장소 쓰기 작업을 하지 않습니다.

OpenAI API 프로젝트에서 키를 발급하고 해당 프로젝트의 결제·모델 접근 설정을 확인한 뒤 `OPENAI_API_KEY`를 설정합니다. 최종 TOP 5에만 근거 추출과 서술 생성을 요청합니다. 정상 실행 시 프로젝트별 2회, 총 10회 요청이며 실패 재시도는 추가될 수 있습니다. Responses API 요청에는 `store=false`와 JSON Schema를 사용합니다.

Telegram에서 BotFather로 Bot을 만들고 받은 토큰을 설정합니다. 개인 대화는 먼저 Bot에 `/start`를 보내고, 그룹/채널에는 Bot을 추가하여 발송 가능한 권한을 설정합니다. Bot API `getUpdates` 등의 결과에서 목적지 `chat.id`를 확인해 `TELEGRAM_CHAT_ID`에 넣습니다. 토큰을 포함한 요청 URL을 채팅·로그·Git에 공유하지 마세요.

Dry Run은 위 Secret 네 개를 요구하지 않습니다. `--preview`는 GitHub/OpenAI 키가 필요하며, `--live`와 `schedule`은 Telegram 설정까지 필요합니다.

## DB 초기화와 수동 실행

```bash
python main.py init-db
python main.py run --preview --candidate-limit 10 --output artifacts/preview
python main.py run --live --output artifacts/live
```

- `init-db`: 운영 SQLite 스키마를 초기화합니다. 기존 테이블 데이터는 유지합니다.
- `--preview`: 실제 GitHub와 OpenAI를 사용하고 로컬 결과만 만듭니다. OpenAI 비용이 발생할 수 있습니다. 운영 DB가 있으면 읽기 전용 복사본으로 비교하며, 운영 이력과 Telegram은 변경하지 않습니다.
- `--live`: 실제 수집·LLM·Telegram 발송을 수행합니다. 운영 DB에 주차를 먼저 확보한 후 처리합니다.
- 후보 기본 상한은 40개이며 `--candidate-limit`은 5~50입니다. 발견된 Critical 후보는 상한 때문에 제거하지 않습니다. API 결과가 부족하면 실제 후보 수는 상한보다 적을 수 있습니다.
- 설정 파일을 지정하려면 `python main.py --env path/to/config.env run --preview`처럼 하위 명령 앞에 지정합니다.

수동 실행은 가장 최근에 종료된 일요일 19:00 KST 주차를 대상으로 합니다. 예를 들어 금요일 실행은 직전 일요일에 끝난 기간을 조사합니다. 현재 Star와 Trending 페이지를 과거 값으로 복원하지 않습니다. 늦은 실행에서는 당시 Trending과 Snapshot 비교가 Unknown일 수 있습니다.

처음 실행해서 비교 Snapshot이 없어도 Release·활동·보안 등 다른 근거로 평가합니다. 근거를 갖춘 프로젝트가 5개 미만이면 TOP 5를 꾸며내지 않고 FAILED로 기록합니다. 수집한 관측치는 다음 분석을 위해 저장하지만 소개 이력과 발송은 만들지 않습니다.

전체 실제 전송 경로를 Cold Start와 무관하게 한 번 검증하려면 수동 전용 integration-test runner를 사용합니다.

```powershell
python scripts\run_full_top5_live.py --integration-test
```

이 명령은 실제 GitHub 후보와 선정된 TOP 5의 실제 OpenAI 분석 및 Telegram 발송을 사용하지만, 과거에 복원할 수 없는 previous snapshot·previous activity·historical Trending만 명시적인 Fixture로 보충합니다. 모든 메시지에는 `[INTEGRATION TEST]`가 표시됩니다. 점수와 TOP 5는 운영 `score_candidate()`와 ranking 규칙으로 계산하며 직접 지정하지 않습니다.

데이터는 운영 `DATABASE_PATH` 대신 `data/full_integration_test.db`에만 저장되고 산출물은 `artifacts/full-integration-test/`에 생성됩니다. 같은 integration-test period는 두 번째 실행 시 GitHub/OpenAI/Telegram 호출 전에 차단됩니다. 운영 Scheduler와 `main.py run --live`에는 이 mode나 Fixture 경로가 없습니다. 다른 완료 period를 명시적으로 시험해야 할 때만 `--period-end 2026-10-11T19:00:00+09:00`처럼 함께 사용합니다.

## Cloudflare R2 SQLite 저장·복원

R2 전송은 `services/r2_persistence.py`의 명시적 API로 제공됩니다. 아래 Production One-shot Runner 및 GitHub Actions 수동/예약 Workflow에서 사용하며 기존 CLI/Pipeline/Scheduler의 동작은 유지합니다. Workflow 요청 시각은 매주 일요일 19:07 Asia/Seoul이며 보고 경계는 19:00으로 유지됩니다. 기본 브랜치 반영 후 적용되며 운영 DB 기본 경로와 기존 7개 테이블 구조를 그대로 사용합니다.

추가 설정은 `.env.example`의 `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`입니다. 의존성은 `python -m pip install -r requirements.txt`로 설치합니다. R2 SDK는 [Cloudflare의 boto3 설정](https://developers.cloudflare.com/r2/get-started/s3/)에 맞춰 S3 endpoint와 region `auto`를 사용합니다. Key/Secret/endpoint 또는 전체 SDK 오류를 로그로 출력하지 않습니다.

API 사용 순서:

1. `settings = Settings.load()` 후 `with R2DatabaseStore(settings) as store:` 문맥에서 `store.restore()`를 호출합니다. DB 연결을 열기 **전**, 로컬 DB가 없는 작업 디렉터리에서 수행합니다. 운영 객체는 `production/github_weekly_report.db`로 고정됩니다.
2. `restore()`는 검증한 파일을 원자적으로 게시하고 `True`를 반환합니다. `NoSuchKey` + HTTP 404인 경우에만 `False`를 반환하며 로컬 DB를 생성하지 않습니다. 그 경우 기존 `Database(settings.database_path)`로 최초 schema를 초기화합니다. `NoSuchBucket`, 인증/권한, 네트워크, 불명확한 404, 손상 DB는 예외로 중단합니다.
3. 기존 로컬 DB 또는 `-wal`/`-shm`/`-journal`이 있으면 restore를 거부합니다. 로컬 claim이나 미반영 WAL을 원격 파일로 덮어쓰지 않습니다. 자동 삭제/복구는 제공하지 않습니다.
4. 실행 완료 후 transaction을 명시적으로 commit/rollback하고 **모든 application DB 연결을 종료한 뒤** `store.upload()`를 호출합니다. 필요하면 `database.sqlite_backup.checkpoint_and_close(db.connection)`으로 WAL checkpoint와 연결 종료를 함께 수행할 수 있습니다. pending transaction은 자동 commit하지 않고 거부하며, busy checkpoint도 업로드를 차단합니다.
5. upload는 integrity/foreign-key/schema 검사를 하고 SQLite backup API로 WAL 없이 사용 가능한 단일 DB를 만듭니다. 기존 원격 운영 DB를 먼저 `backups/<UTC timestamp>-<UUID>.db`로 server-side copy한 뒤 새 DB를 업로드합니다. 백업 실패 시 운영 객체를 덮어쓰지 않습니다. 최초 운영 객체가 없는 경우에만 백업을 생략합니다. 반환값은 백업 key 또는 최초 업로드의 `None`입니다.

`R2DatabaseStore` import는 `from services.r2_persistence import R2DatabaseStore`, 설정은 `from config.settings import Settings`입니다. 전송용 임시 파일·스트림·SQLite 연결·자체 생성 SDK client는 종료 시 정리됩니다. 주입한 Mock/client의 종료 책임은 호출자에게 있습니다.

운영 객체 전송은 설정의 `DATABASE_PATH` 한 경로에만 묶입니다. test/integration/preview/fixture DB 파일명, DB 내부의 non-live run mode, fixture snapshot/metric 출처가 있으면 업로드를 거부합니다. 파일명을 production DB처럼 바꿔도 integration-test 이력이 있으면 차단하며 restore에도 동일한 내부 검사를 적용합니다. 운영 DB에 테스트 이력을 섞지 마세요.

R2 전송 자체는 여러 작업 간 분산 잠금을 제공하지 않습니다. Workflow concurrency가 download→run→upload 전체를 직렬화하며, 별도 서버의 Runner를 동시에 실행하지 않아야 기존 SQLite claim이 원격 환경에서도 유지됩니다. 이 모듈의 테스트는 실제 R2 호출 없이 `python -m pytest -q tests/test_r2_persistence.py`로 실행합니다.

## 기존 로컬 운영 DB의 R2 최초 이전

GitHub Actions validate 성공 후 **기존 운영 DB를 이전**할 때 사용합니다. 기존 `upload()`는 운영 객체를 교체하므로 최초 이전에는 사용하지 않습니다. DB를 새로 초기화하는 One-shot Runner의 `--allow-initial-bootstrap`과도 별개입니다.

모든 로컬 DB writer/Scheduler를 중지하고 Actions production 실행도 중지한 상태에서, 일반 Windows PowerShell의 프로젝트 루트에서 실행합니다. `.env`에 `DATABASE_PATH=data/github_weekly_report.db` 및 `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`을 설정합니다. GitHub에 등록한 Secrets는 로컬 PowerShell에 자동 전달되지 않습니다. 기존 OS 환경변수는 `.env`보다 우선하므로 의도한 운영 경로를 확인하세요. GitHub/OpenAI/Telegram 설정은 이 명령에 필요하지 않습니다.

```powershell
Set-Location C:\Github-weekly-report
python -m pip install -r requirements.txt
python scripts/bootstrap_r2_database.py
```

명령은 schema/integrity/foreign key 및 기존 non-live/fixture 검사를 재사용합니다. 원격 `production/github_weekly_report.db`가 존재하면 거부하며, 인증·권한·네트워크 오류나 불명확한 404도 최초 실행으로 취급하지 않습니다. 없는 객체만 `If-None-Match: *` 조건부 PUT으로 생성하므로 확인 직후 다른 실행이 객체를 생성해도 덮어쓰지 않습니다. [R2는 이 조건부 생성을 원자적으로 처리합니다](https://developers.cloudflare.com/r2/api/s3/api/).

WAL checkpoint 후 연결을 닫고 SQLite backup API로 단일 파일을 export합니다. 원본 DB/claim/history를 삭제하거나 교체하지 않으며, 검증용 로컬 백업은 DB 옆 `r2-bootstrap-backups/<UTC시각>-<UUID>.db`에 보존합니다. 체크포인트는 정상적인 WAL 내용을 원본 DB에 반영할 수 있습니다. export 실패 시에도 남은 파일을 자동 삭제하지 않으므로 복구에 쓰기 전 무결성을 검사해야 합니다. 최초 객체라 이전 원격 백업은 없으며, 이후 정상 운영의 기존 `upload()`가 `backups/`에 원격 백업을 만듭니다.

업로드 후 객체를 다시 읽어 존재 여부·ContentLength·실제 byte 수·SHA-256을 로컬 백업과 비교한 경우에만 `Initial R2 migration: SUCCESS` 및 종료 코드 0을 출력합니다. 실패는 종료 코드 1이며 강제 덮어쓰기/삭제 옵션은 없습니다. timeout 또는 검증 실패 때는 이미 원격에 생성됐을 가능성이 있으므로 원본과 백업을 보존하고 원격 상태를 확인하세요. 재실행해도 존재하는 객체를 교체하지 않습니다.

이 명령은 R2만 호출하며 GitHub 수집/LLM/Telegram/Pipeline/운영 claim 갱신/스케줄 등록을 하지 않습니다. Codex에서는 Mock 검증만 수행했습니다. 관련 테스트: `python -m pytest -q tests/test_r2_bootstrap.py tests/test_r2_persistence.py`. 기존 운영 DB의 최초 이전과 Actions production 연결 검증은 사용자 확인으로 완료되었으므로 정상 운영 중 bootstrap을 다시 실행하지 않습니다. 정규 예약은 아래 Workflow에서 관리합니다.

## R2 Production One-shot Runner

저장소 루트의 새 작업 환경에서 다음 명령을 실행합니다. 기본 로컬 DB는 `data/github_weekly_report.db`이고 설정의 `DATABASE_PATH`를 사용합니다. 기존 DB 또는 WAL이 있는 작업 폴더는 복원 보호에 의해 차단되며 Runner가 삭제하거나 덮어쓰지 않습니다.

```powershell
python scripts/run_weekly_live_once.py
```

최초 R2 운영 객체가 아직 없다는 사실을 확인했고 신규 운영 DB 초기화를 의도할 때만 다음 옵션을 사용합니다.

```powershell
python scripts/run_weekly_live_once.py --allow-initial-bootstrap
```

이 옵션도 인증/권한/네트워크 오류나 로컬 DB 존재 보호를 우회하지 않습니다. `NoSuchKey` + HTTP 404일 때만 신규 DB를 초기화합니다. integration-test/dry-run/preview/fixture, 과거 period override 및 force/reclaim 옵션은 제공하지 않습니다. 복원된 DB에 non-production run/fixture가 있어도 Pipeline 진입 전에 차단합니다.

Runner는 설정 검증→R2 복원→기존 Scheduler의 `report_period()`로 기간 확정→live claim 사전 확인→Production Pipeline 한 번→SQLite checkpoint/close/integrity/schema 검사→R2 백업/업로드→종료 순서로 동작합니다. 대기 루프가 없습니다. 기준 시각은 시작 시 한 번 고정하고 보고 종료 시각은 가장 최근 **완료된** 일요일 19:00 KST입니다. 일요일 19:00 이전 수동 실행은 직전 일요일에 끝난 주차를 선택하며, 지연 실행도 종료 시각을 실제 실행 시각으로 바꾸지 않습니다. 조사 기간은 168시간 반개방 구간입니다.

기존 SUCCESS/PARTIAL_SUCCESS/FAILED/RUNNING claim이 같은 종료 시각에 있으면 기존 row를 변경하지 않고 수집/OpenAI/Telegram client를 만들기 전에 차단합니다. Pipeline의 기존 원자적 claim 검사는 그대로 추가 적용됩니다. 정상 terminal 결과 및 처리된 `InsufficientCandidates`만 DB에 terminal claim/finished_at이 존재하는지 확인하고 업로드합니다. 예상 밖 예외는 Pipeline이 FAILED를 기록했더라도 자동 업로드하지 않습니다. 미완료 transaction은 임의 commit하지 않고 업로드를 막습니다.

| 종료 코드 | 최종 상태 | R2 업로드 |
|---|---|---|
| 0 | `SUCCESS` | 검증 후 완료 |
| 0 | `SKIPPED_ALREADY_CLAIMED` | 생략; 기존 claim 유지 |
| 1 | `APPLICATION_FAILED` | terminal FAILED 기록 검증 후 완료 |
| 2 | `PARTIAL_SUCCESS` | 부분 성공 기록 검증 후 완료 |
| 3 | 설정/시작/복원 실패 또는 `BOOTSTRAP_REQUIRED` | 수행하지 않음 |
| 4 | `SQLITE_FINALIZE_FAILED` / `RUN_STATE_INVALID` | 차단 |
| 5 | `R2_UPLOAD_FAILED` | 백업/업로드 단계 실패; 성공으로 처리하지 않음 |
| 6 | `UNEXPECTED_FAILURE` | 예상 밖 오류 후 추가 업로드하지 않음 |
| 130 | `INTERRUPTED` | 복구 목적의 추가 업로드 없음 |

Telegram 전송 성공 후에도 R2 업로드가 확인되지 않으면 전체 성공이 아닙니다. 업로드 응답 실패는 원격 반영 여부가 불확실할 수도 있습니다. 로컬 DB/WAL, `artifacts/weekly-live-once/<period-end-date>/`, `logs/weekly_report.log`를 보존하고 원격 claim/전송 로그를 확인해야 합니다. 강제 종료/예상 밖 예외로 claim을 R2에 보존하지 못한 경우도 동일합니다. Runner는 임의 claim 삭제나 발송 재시도를 수행하지 않습니다. bootstrap 옵션을 일반 자동 재시도 수단으로 사용하지 마세요.

환경변수는 기존 `GITHUB_TOKEN`, OpenAI/Telegram 설정과 R2 설정을 사용합니다. Workflow에서 repository Secret `GH_API_TOKEN`을 환경변수 `GITHUB_TOKEN`으로 연결합니다. Workflow는 restore→run→upload 전체에 **production 전용 concurrency group**과 **cancel-in-progress: false**를 적용하며, 별도 서버의 실행까지 자동 직렬화되는 것은 아닙니다. 한 운영 writer만 허용해야 합니다. 실행 중 강제 종료나 원격 업로드 실패의 exactly-once 보장은 concurrency만으로 해결되지 않으므로 자동 재발송 대신 상태를 점검합니다.

관련 테스트는 `python -m pytest -q tests/test_weekly_live_once.py tests/test_r2_persistence.py`이며 실제 GitHub/OpenAI/Telegram/R2 호출은 없습니다. Runner 단독 검증에는 Scheduler 실행과 실제 API 테스트를 포함하지 않습니다.

Production `InsufficientCandidates` 발생 시 콘솔에서 Candidate funnel / Exclusion reasons / Data gaps를 확인합니다. 신규 실패 run의 기존 `automation_runs.report` JSON에도 안전한 집계가 보존됩니다. [기간·Cold Start·진단 해석 및 운영 판단](docs/PRODUCTION_CANDIDATE_DIAGNOSTICS.md)을 참고하세요. 과거 실패 claim을 지우거나 같은 주차를 재실행하여 진단을 재생성하지 않습니다.

## GitHub Actions 수동 검증 및 운영 활성화

`.github/workflows/weekly-report.yml`은 Ubuntu GitHub-hosted Runner에서 Python 3.11을 설정하고 requirements 설치 및 `pip check` 후 설정 검사를 실행합니다. **정규 Workflow 요청은 매주 일요일 19:07 Asia/Seoul**입니다. 정각 부하를 피하기 위한 완화이며 실제 실행 시각은 보장하지 않습니다. Workflow 파일을 기본 브랜치에 반영해야 GitHub 예약에 적용됩니다. 수동 실행은 **Actions → GitHub Weekly Report → Run workflow**에서 기존 mode를 선택합니다.

- `validate`(기본값): Secret 존재 여부/설정 형식, production 경로, 프로젝트 파일, SDK import를 검사하고 compile/전체 pytest를 수행합니다. DB를 열거나 R2 client를 만들지 않고 수집/OpenAI/Telegram/R2 애플리케이션 API를 호출하지 않습니다. GitHub-hosted 환경 준비·의존성 설치에는 네트워크가 필요합니다. 인증 성공/원격 DB 존재 여부는 이 모드로 검증하지 않습니다.
- `production`: 위 offline 설정 검사를 통과한 뒤 `python scripts/run_weekly_live_once.py`를 실행합니다. 실제 GitHub/OpenAI/Telegram/R2 API를 사용하며 발송할 수 있습니다. 자동 bootstrap은 없고, Runner의 nonzero 종료 코드는 Workflow 실패로 유지됩니다. 기존 claim이면 Runner가 API client 생성 전에 차단하고 안전하게 종료합니다.
- `schedule` 이벤트: dispatch 입력값 없이 위 production 경로를 실행합니다. 수동 validate 전용 pytest step은 건너뛰며 설정 검사는 먼저 실행합니다. Python Scheduler 대기 루프를 실행하지 않습니다.

Secret은 설치/pytest step에 전달하지 않고, offline 설정 검사와 production 실행 step에만 다음처럼 연결합니다. 값은 출력하지 않습니다.

| Repository Secret | Python 환경변수 |
|---|---|
| `GH_API_TOKEN` | `GITHUB_TOKEN` |
| `OPENAI_API_KEY` | `OPENAI_API_KEY` |
| `TELEGRAM_BOT_TOKEN` | `TELEGRAM_BOT_TOKEN` |
| `TELEGRAM_CHAT_ID` | `TELEGRAM_CHAT_ID` |
| `R2_ENDPOINT` | `R2_ENDPOINT` |
| `R2_ACCESS_KEY_ID` | `R2_ACCESS_KEY_ID` |
| `R2_SECRET_ACCESS_KEY` | `R2_SECRET_ACCESS_KEY` |
| `R2_BUCKET` | `R2_BUCKET` |

DB 경로는 `data/github_weekly_report.db`, TIMEZONE은 `Asia/Seoul`로 고정 주입합니다. 기존 실행 스크립트가 project root를 import 경로에 추가하므로 별도 PYTHONPATH 변경은 없습니다. concurrency group은 `github-weekly-report-production`, `cancel-in-progress: false`, `queue: max`이며 validate와 production 모두 같은 Workflow 경계 안에서 직렬화합니다. 대기열 보존은 [GitHub concurrency 공식 문서](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)를 기준으로 적용했습니다.

`DATABASE_PATH`, `TIMEZONE`, `OPENAI_MODEL`은 설정 검사와 production 실행 step에만 주입합니다. Offline pytest에는 운영 설정을 상속시키지 않습니다. Settings의 기존 우선순위는 **OS 환경변수 > `--env`로 지정한 파일 > 기본값**이며 `--env`가 OS 값을 강제로 덮어쓰는 옵션은 아닙니다. 파일 설정 자체를 검사하는 CLI 테스트는 외부 DATABASE_PATH를 monkeypatch로 격리하고, 환경변수 우선순위는 별도 테스트로 검증합니다.

Job timeout은 60분으로, 수집·최대 TOP 5의 OpenAI 2단계·Telegram retry 및 R2 업로드를 포함한 전체 실행을 제한합니다. timeout/취소 시 자동 복구 발송이나 finally 업로드를 하지 않습니다. 마지막 Job Summary에는 dependencies/config/tests/production의 고정 step outcome만 기록하고, 파일 본문이나 Secret은 포함하지 않습니다. 원본 logs/artifacts, `.env`, SQLite DB/WAL/SHM은 Actions Artifact로 **업로드하지 않습니다**. package cache는 pip 캐시만 사용합니다. 운영 DB 영구 저장은 R2만 담당합니다. 업로드 실패/강제 종료 뒤 hosted runner의 로컬 DB·로그는 보존되지 않으므로 원격 claim/발송 여부를 확인하기 전 재실행하지 마세요.

기본 브랜치 반영 후 확인 절차:

1. 예약 활성화 변경을 기본 브랜치에 Push/merge하고 Workflow가 enabled 상태인지 확인합니다. 최신 validate 통과 및 R2 최초 이전/운영 연결 검증은 선행 완료 조건입니다.
2. 필요하면 기본 mode `validate`로 수동 재검증합니다. 이 선택은 production을 실행하지 않으며 정규 예약은 계속 활성 상태입니다.
3. 다음 일요일 Actions run의 이벤트가 `schedule`이고 `Run production once`가 실행됐는지 확인합니다. 다른 서버의 Python Scheduler/cron을 동시에 실행하지 않습니다.
4. 콘솔의 보고 기간, Claim, Pipeline 결과, SQLite finalize, R2 upload 및 최종 종료 상태를 확인합니다. 성공한 보고서는 Telegram 수신도 확인합니다. 이미 Claim된 주차를 강제로 재실행하지 않습니다.

```yaml
  schedule:
    - cron: "7 19 * * 0"
      timezone: "Asia/Seoul"
```

예약 요청은 일요일 19:07 KST이며 [GitHub schedule timezone 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)의 구문을 사용합니다. schedule 이벤트는 production 경로를 선택하고, 예약이 지연돼도 기존 Runner가 가장 최근 완료된 일요일 **19:00** 경계를 사용합니다. 기간은 `이전 일요일 19:00 <= event < 이번 일요일 19:00`의 168시간입니다. 다른 서버/Workflow가 동일 R2 객체를 쓰면 같은 저장소의 concurrency만으로는 보호되지 않으므로 운영 writer를 단일화합니다.

Runner는 실제 KST 시작/restore 후 preflight 시각과 보고 종료 후 경과 시간을 출력하고 Trending/Snapshot의 시간상 허용 여부를 표시합니다. 기존 3600초 제한은 유지하며 이전 Snapshot이 없는 경우도 계속 unknown입니다. Snapshot의 신규 measured_at은 metadata 배치 완료 시각이고, 비교 불가 사유는 현재/이전 관측 window 초과로 구분합니다. [지연 원인 점검과 후속 관측 A/B/C 설계](docs/DELAY_AND_OBSERVATION_PLAN.md)를 참고하세요. 별도 관측 Workflow/DB schema 변경은 아직 구현하지 않았습니다.

`InsufficientCandidates`이면 Candidate funnel / Exclusion reasons / Data gaps를 확인합니다. Cold Start 또는 경계 후 1시간 초과 지연으로 Trending/Star 비교 근거가 부족할 수 있으며, 기준을 완화해 보고서를 채우지 않습니다. FAILED terminal claim 및 R2 upload 성공 여부를 확인하고 해당 주차 claim은 유지합니다. 예외/취소/timeout/업로드 실패 시 원격 claim과 Telegram 발송 여부를 먼저 확인하고 자동 재발송하거나 claim을 삭제하지 않습니다. 진단 집계는 신규 실패 run의 기존 DB JSON에도 보존됩니다.

구조/안전 경계 테스트는 `python -m pytest -q tests/test_weekly_workflow.py tests/test_scheduler.py`로 실행합니다. 이번 변경은 예약 정의를 활성화했으며 Codex에서 Workflow dispatch나 실제 API/production 실행은 하지 않았습니다.

Offline configuration step이 실패하면 콘솔의 아래 코드를 확인합니다. 변수명/고정 category만 출력하며 Secret 값, 일부 문자, 길이, 접두사, 예외 원문은 출력하지 않습니다. 여러 설정 오류는 함께 표시합니다.

| 진단 | 확인 사항 |
|---|---|
| `MISSING_ENV: <변수명>` | 위 매핑 표에 맞는 Repository Actions Secret이 존재하고 해당 step에 전달되는지 확인합니다. `GITHUB_TOKEN` 누락이면 Secret 이름은 `GH_API_TOKEN`입니다. |
| `INVALID_FORMAT: R2_ENDPOINT` | 기존 Settings 규칙상 HTTPS S3 base endpoint여야 합니다. Bucket 경로, query, fragment, user/password를 포함할 수 없습니다. Root의 `/`는 허용됩니다. 값을 잘라 자동 보정하지 않습니다. |
| `INVALID_FORMAT: DATABASE_PATH` | Workflow에서 고정한 `data/github_weekly_report.db`인지 확인합니다. 실제 설정 경로 값은 로그에 출력하지 않습니다. |
| `INVALID_FORMAT: TIMEZONE` | `Asia/Seoul`이어야 합니다. |
| `LOCAL_DB_EXISTS: DATABASE_PATH` | checkout에 운영 DB가 섞였거나 앞 단계가 생성했는지 확인합니다. 파일을 자동 삭제하지 않습니다. |
| `LOCAL_WAL_EXISTS`, `LOCAL_SHM_EXISTS`, `LOCAL_JOURNAL_EXISTS` | 같은 운영 DB의 sidecar가 존재합니다. 원본 파일을 보존하고 출처를 점검합니다. |
| `SETTINGS_VALIDATION_FAILED: LIVE_SETTINGS` / `R2_SETTINGS` | 해당 Settings 검증기의 예상 밖 오류입니다. 예외 원문은 숨깁니다. |
| `SETTINGS_VALIDATION_FAILED: SETTINGS_LOAD` | Settings 로딩 단계에서 실패했습니다. `.env`/환경설정의 timezone 및 빈 DB 경로 등을 확인합니다. |
| `SETTINGS_VALIDATION_FAILED: LOCAL_STATE_CHECK` / `WORKING_DIRECTORY` / `UNEXPECTED_VALIDATOR_ERROR` | 파일 접근/실행 위치/검증기 오류를 확인합니다. |

현재 `.gitignore`의 `data/*`는 운영 DB와 WAL/SHM/journal을 모두 제외하고 `.env`도 별도로 제외합니다. Git에 이미 추적된 파일에는 ignore가 소급 적용되지 않으므로 `git ls-files -- '*.db' '*.sqlite' '*.sqlite3' '*-wal' '*-shm' '*-journal' '.env'`로 이름만 확인합니다. 추적 파일이 있으면 로컬 원본을 보존한 뒤 검토를 거쳐 Git 추적만 해제해야 하며, validator는 삭제/덮어쓰기/추적 해제를 수행하지 않습니다.

수정본을 Push한 뒤 **Actions → GitHub Weekly Report → Run workflow → 수정이 반영된 브랜치 → mode=validate**로 새 검증을 실행합니다. 검증은 실제 서비스 인증/발송/운영 DB 변경 없이 수행하며, 진단이 사라졌다고 해서 production의 R2 연결이나 발송 성공이 보장되는 것은 아닙니다. Production 모드에도 동일한 필수 환경변수와 기존 설정 검증을 적용합니다.

## 비용 없는 Dry Run

모든 Repository·사건·수치·LLM 응답은 명시적인 합성 Fixture입니다. HTTP 요청은 `httpx.MockTransport` 안에서만 처리되며 실제 API 호출과 Telegram 발송은 없습니다. 운영 DB와 별도의 `출력폴더/dry-run.sqlite`를 사용합니다.

개발용 저비용 검증:

```bash
python main.py run --dry-run --candidate-limit 10 --structure-only --at 2026-10-04T19:00:00+09:00 --output artifacts/limited
python main.py run --dry-run --candidate-limit 10 --resume-candidates artifacts/limited/candidates.json --at 2026-10-04T19:00:00+09:00 --output artifacts/limited
python main.py format artifacts/limited/report.json --output artifacts/rendered
```

첫 명령은 후보 수집·비교·점수·TOP 5까지만 검증합니다. 두 번째는 같은 후보 데이터를 재사용해 TOP 5만 심층 분석합니다. 세 번째는 저장된 리포트를 재포맷하므로 GitHub와 LLM을 다시 호출하지 않습니다. `--at`, `--structure-only`, `--resume-candidates`는 Dry Run 전용입니다.

전체 규모 검증은 마지막에 전용 출력 폴더로 **1회** 실행하고, 문제가 난 단계만 따로 재검증합니다.

```bash
python main.py run --dry-run --candidate-limit 40 --at 2026-10-04T19:00:00+09:00 --output artifacts/full-dry-run
python -m scripts.verify_run artifacts/full-dry-run
```

이 저장소 작업에서는 위 전체 규모 실행을 이미 한 번 수행했습니다. `verify_run`은 기존 산출물과 SQLite만 검사하고 수집·생성·전송을 반복하지 않습니다. 자세한 수치는 [검증 기록](docs/VALIDATION_REPORT.md)에 있습니다.

## 테스트

```bash
python -m pytest -q
python -m pytest -q tests/test_scoring.py
python -m pytest -q tests/test_integration.py tests/test_pipeline_safety.py
```

전체 테스트는 네트워크 socket 연결을 금지합니다. 점수 경계, Unknown/0, 중복·재선정, Critical, 저장 원자성, 주차 동시 실행, Telegram 분할·재시도·부분 실패, API Mock 통합을 검증합니다. 기본 pytest는 후보 40개 전체 규모 실행을 다시 수행하지 않습니다.

## 매주 일요일 19:00 KST 실행

포그라운드 스케줄러:

```bash
python main.py schedule --output artifacts/scheduled
```

프로세스가 실행 중이어야 합니다. 시작 시 이미 지난 주차를 즉시 재발송하지 않고 다음 일요일 19:00을 기다립니다. 서버의 로컬 시간대와 무관하게 KST 경계를 계산합니다. 중지하려면 Ctrl+C를 사용합니다.

운영체제 scheduler로 `run --live`를 주 1회 호출해도 됩니다. 예를 들어 **UTC 시간대인 Linux 서버**의 crontab:

```cron
0 10 * * 0 cd /path/to/Github-weekly-report && /path/to/Github-weekly-report/.venv/bin/python main.py run --live --output artifacts/weekly >> logs/scheduler.log 2>&1
```

Windows 작업 스케줄러는 OS 시간대를 `대한민국 표준시`로 설정한 후 매주 일요일 19:00 트리거를 만듭니다. 프로그램은 가상환경의 `python.exe` 절대 경로, 인수는 `main.py run --live --output artifacts/weekly`, 시작 위치는 저장소 절대 경로로 지정합니다. 계정이 `.env`, DB, 출력·로그 폴더에 접근할 수 있어야 합니다.

저장소에는 스케줄러 구현만 제공하며, 이 개발 세션에서 OS 작업이나 상주 프로세스를 설치·실행하지 않았습니다.

조사 기간은 정확히 `이전 일요일 19:00 이상 ~ 이번 일요일 19:00 미만`입니다. 끝 시각과 정확히 같은 이벤트는 다음 주에 포함합니다. 이벤트 시각은 168시간 반개방 구간으로 필터링합니다.

## 중복 실행·전송·복구

운영 실행은 SQLite `BEGIN IMMEDIATE`로 주차를 확보합니다. 같은 조사 종료 시각의 live 실행 이력이 있으면 상태에 관계없이 추가 수집·발송 전에 차단합니다. UTC와 KST로 표기한 같은 시각도 동일 주차로 취급합니다. cron/manual 중복과 프로세스 재시작에 따른 자동 재발송을 막기 위한 보수적인 정책입니다.

각 Telegram 메시지는 최초 시도 + 재시도 2회, 총 3회까지만 처리합니다. 최종 실패해도 다음 메시지로 진행하며 모든 시도를 DB에 기록합니다. 성공과 실패가 섞이면 PARTIAL_SUCCESS입니다. 실패 메시지를 다음 주에 자동 재전송하지 않습니다. API 응답을 받기 전에 연결이 끊어진 경우에는 Bot API가 이미 수신했는지 확인할 수 없어 개별 재시도의 중복까지 완전히 보장하지는 못합니다.

프로세스가 비정상 종료되면 `automation_runs`에 RUNNING이 남을 수 있습니다. 자동으로 실행 이력을 삭제하거나 다시 보내지 않습니다. 우선 해당 주차의 `report.json`, `telegram_logs`, 로그 파일을 확인하세요. 출력 확인에는 `format`을 사용합니다. 운영 실행 재개가 필요하면 이미 전송한 범위를 확인한 뒤 별도 운영 판단이 필요하며 자동 재개 기능은 제공하지 않습니다.

프로세스 종료 코드: `0` 성공, `1` 설정/실행 오류, `2` 분석 완료 후 부분 성공 또는 전송 실패, `3` 중복 주차 차단, `130` 사용자 중단입니다.

## 산출물과 로그

- `출력폴더/candidates.json`: 수집 경로, 지표와 상태, 점수, 제외 이유를 포함한 후보 감사 자료
- `출력폴더/report.json`: TOP 5·근거·서술·Watchlist·주간 흐름; 재포맷 입력
- `출력폴더/messages.txt`: 분할 및 순서가 적용된 실제 발송 텍스트
- `출력폴더/selection.json`: 구조 검증 전용 결과
- `출력폴더/verification.json`, `verification_checks.json`: Dry Run 및 사후 검사 결과
- `logs/weekly_report.log`: API 소스 실패, 단계 실패 유형, Run ID, 완료 상태
- `automation_runs`: 주차, 실행 모드, 전체 상태, 종료 시각, 결과
- `telegram_logs`: 메시지 종류, TOP 순위, 분할 번호, 시도 횟수, 성공/재시도/실패, 오류, 시각

로그는 설정 Secret을 마스킹하고 HTTP 라이브러리의 요청 URL 정보 로그를 비활성화합니다. `.env`, `data/`, `logs/`, `artifacts/`, `.venv/`의 실행 데이터는 Git에서 제외합니다. SQLite 파일은 운영 백업 대상으로 관리하세요.

## 구조와 규칙 수정 위치

| 경로 | 역할 |
|---|---|
| `main.py` | CLI 진입점 |
| `config/settings.py` | 환경 설정 로딩·Secret 표현 제한 |
| `config/rules.py` | 점수 버킷, 후보/근거 기준, 수집 한도, 긴급도 보조 기준 |
| `collectors/` | GitHub REST/GraphQL, Trending, Release/Issue/Advisory 파싱 |
| `models/domain.py`, `serialization.py` | 도메인 모델·JSON 저장/복원 |
| `database/` | SQLite 스키마, CRUD, 주차 claim, 전송 로그 |
| `services/` | 비교·점수·검증·선정·심층 수집·근거·서술·저장·Pipeline·CLI·스케줄러 |
| `services/demo.py` | 오프라인 합성 HTTP Fixture |
| `telegram/` | 고정 포맷, 섹션/문단 분할, 독립 재시도 발송 |
| `prompts/` | 근거 추출과 한국어 서술 지시; Telegram 레이아웃과 분리 |
| `tests/` | 외부 연결 없는 단위/제한 통합 테스트 |
| `scripts/` | Phase 체크포인트 및 저장된 전체 Dry Run 검증 |

핵심 정책:

- Star 절대 증가 기준은 현재 저장소 규모로 선택하며 신규 30일 이내 증가율 점수는 최대 16점입니다.
- Unknown 점수는 `None`으로 보존하고 확인한 항목만 합산합니다. 미확인 항목을 0으로 저장하거나 총점을 100점으로 재정규화하지 않습니다.
- Issue와 PR은 서로 겹치지 않는 검색 수를 합산합니다. 이전 수가 0이면 증가율은 `not_applicable`입니다.
- Fork/Discussion은 총량 Snapshot 변화의 수치 보조 지표입니다. Discussion 순증은 신규 게시물 수와 다르며 삭제 영향이 있을 수 있습니다. 정성 점수의 수치 환산은 `COMMUNITY_GROWTH_BUCKETS`에서 관리합니다.
- 일반 후보는 독립 신호 두 개 이상을 요구합니다. Star 절대 증가와 증가율은 한 신호입니다. 공식 Critical 보안은 단일 근거 예외입니다.
- Repository ID 우선으로 중복을 합치고 여러 사건의 대표 점수는 최댓값을 사용합니다. 분야별 할당량은 없습니다.
- 공식 Release/보안, 독립 신호 수, 확인된 지표를 함께 검증합니다. Critical은 필수 포함하고 확인 가능한 공격 용이성·프로젝트 규모 등의 긴급도 보조 기준으로 순서를 조정합니다. 일반 화제성 점수 자체는 바꾸지 않습니다.
- 서로 다른 Critical 저장소가 5개보다 많으면 ‘TOP 5’와 ‘모두 포함’이 충돌하므로 오류로 남깁니다.
- 이전 소개 이후 새로운 이벤트 또는 새로운 주간 Star 급증이 있어야 재선정합니다. 변경 근거를 서술에 포함합니다.
- 전주와 현재 Snapshot의 실제 측정 시각이 각 주차 종료 후 1시간 이내인 경우만 주간 변화로 비교합니다. 관측 간격은 수집 지연에 따라 수분 차이가 날 수 있으며 실제 시각을 보관합니다. 과거 주차에 늦게 측정한 값을 소급 추정하지 않습니다.

## 운영 초기 확인 사항

GitHub Trending은 공식 과거 순위 API가 아니므로 현재 weekly 페이지의 관측 순위만 사용하며, 페이지 변경/수집 실패/과거 기간에는 Unknown으로 둡니다. API 페이지 한도에 도달하거나 검색이 불완전하면 값을 확정하지 않습니다. Repository와 연결할 수 없는 Advisory는 임의 패키지 매핑을 하지 않습니다.

Release와 정책 판정은 공식 버전·명시된 문서 헤더·작성자 관련성에 기반한 보수적인 규칙입니다. 일부 형식이나 언어의 사건은 자동 감지하지 못할 수 있습니다. 공식 문서 수집은 README와 저장소 `docs/index.md`를 우선하며 외부 문서 사이트를 무제한 크롤링하지 않습니다.

심층 분석은 TOP 5만 수행합니다. 프로젝트당 Issue 3개, Discussion 2개, PR 2개, Release 2개로 제한하고 원문 길이·총량도 제한합니다. 일부 댓글이나 문서는 수집 범위 밖일 수 있습니다. 의견 출처와 인용은 검증하지만 요약의 의미적 정확성을 완전히 자동 보증하지는 않습니다. 실제 API 설정 후 초기 3~4주간 후보 품질·반복 소개·서술 근거·Telegram 길이를 확인하세요.

참고 API 문서: [GitHub 공개 Advisory](https://docs.github.com/en/rest/security-advisories/global-advisories), [GitHub Discussions](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions), [OpenAI 구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs), [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage).

## 저장 관측값의 Production 연결 — Phase 5B

기본 Production factory에 읽기 전용 Observation Consumer를 코드상 연결했습니다. 이번 단계에서는 실제 실행·배포를 수행하지 않았으며 관측 Workflow와 Pool 발행 운영 경로는 아직 없습니다. 원자적 주차 Claim 획득 후 discovery/history 조회를 마친 뒤 Consumer를 한 번 실행합니다. 기존 Claim이면 관측 R2 List/GET도 수행하지 않습니다. 직접 생성한 Pipeline은 loader 주입이 없으면 기존 경로를 유지하며 dry-run/preview/integration-test에서는 주입된 loader도 실행하지 않습니다.

Scoring의 규모별 T와 Star/Fork 증가량은 검증된 경계 관측값을 사용합니다. 이전1000/경계1200/최신1500이면 delta200·growth20%이며 최신1500과 실제 discovery 배치 완료시각은 별도 보존합니다. Telegram에는 최신값과 평가 기준값·관측시각을 구분해 표시하고 SQLite Snapshot/Repository/Watchlist에는 최신값을 저장합니다. 경계 원본은 R2 append-only 객체에 남습니다.

관측 객체 우선, 유효 Legacy fallback 순서이며 동시각 충돌은 먼저 차단합니다. 기존 SQLite 행에 요청 시작 시각이 없으면 추정하지 않고 Legacy 비교에서 제외합니다. 관측 누락/손상/R2 접근 실패는 해당 평가 지표를 unknown으로 만들고 다른 실제 신호로 평가를 계속합니다. Evidence 최소2개/Critical 예외/점수표/중복 발송 정책은 유지합니다. 후보 부족 시 TOP5를 채우지 않습니다.

Actions 콘솔의 `Observation diagnostics`와 automation_runs.report의 `observation_diagnostics`에서 current/previous 확인, 비교 가능, Trending, conflict/unavailable, Legacy 사용 집계를 확인합니다. 기존 funnel과 별도 집계입니다. Phase6에서 Producer→공유 Fake R2→Consumer→주간 Runner 전체 Mock E2E를 완료했습니다. 실제 R2 호환성·운영 API/관측 Workflow 배포는 Phase7에서 별도 검증해야 합니다.

오프라인 연결 검증: `python -m pytest tests/test_observation_pipeline.py -q`. 상세 데이터 계약과 후속 단계는 docs/OBSERVATION_ARCHITECTURE.md 16절 및 docs/IMPLEMENTATION_STATUS.md를 참조하세요.

관측 전체 E2E: `python -m pytest tests/test_observation_e2e.py -q` (43개). Pool Builder/Job/Store/Consumer/Pipeline/SQLite/R2 Persistence를 실제 코드로 실행하며 HTTP/S3만 Fake입니다. 관측 객체는 fixture 표시와 test namespace를 유지하고 별도 테스트 factory만 fixture namespace 검증기를 주입합니다. 기본 Production 검증기는 fixture를 계속 거부합니다. 모든 DB는 pytest 임시 디렉터리, 버킷은 공유 메모리 객체이며 socket·실제 HTTP transport·SDK factory·설정 Secret 로딩을 차단합니다. 상세 장애 행렬은 설계17절을 참조하세요. 전체 회귀732개 통과이며 Phase7 배포는 미완료입니다.

## Observation 배포 사전 검사 — Phase 7A

`.github/workflows/observation.yml`의 **Observation System**은 `workflow_dispatch`의 `mode=validate`만 지원합니다. schedule/workflow_run/Production 단계가 없고 기존 Weekly Report와 자동 연계하지 않습니다. 서비스 Secret을 주입하지 않으며 권한은 contents: read, concurrency는 별도 `github-weekly-report-observation`, cancel-in-progress=false/queue=max, Ubuntu/Python3.11/timeout20분입니다. 기존 Weekly Report Workflow는 변경하지 않았습니다.

검사는 dependency 설치 → 구조 검증기 → Observation 테스트 → 전체 pytest → compile → diff 순서이며 실패를 무시하지 않습니다. Summary에는 고정 단계 outcome만 기록하고 DB/로그/Secret Artifact를 업로드하지 않습니다. `.env`/운영 DB/실제 API 없이 모델·설정 필드·인터페이스·기본 규칙·의존성을 확인합니다. **R2 인증·접속·조건부 PUT 호환성이나 실제 Production 설정값을 검증한 결과는 아닙니다.**

로컬 검증: `python scripts/validate_observation_deployment.py`, `python -m pytest tests/test_observation_workflow.py -q`. 기본 브랜치에 사용자가 변경사항을 반영한 뒤 Actions → Observation System → Run workflow → mode=validate를 선택하고 모든 단계와 Summary의 Production NOT EXECUTED를 확인합니다. 이번 작업에서 Commit/Push/Dispatch는 수행하지 않았습니다. Phase7A 실행을 위해 새 Secret을 추가할 필요는 없습니다.

Phase7B는 아래 전용 테스트 probe의 코드/Mock 검증까지 준비되었습니다. 실제 R2 실증은 NOT TESTED이며7C~7E는 미구현입니다. 기존 Production Secret/Consumer 설정은 바꾸지 않았습니다. 후속 게이트는 설계19절과 IMPLEMENTATION_STATUS.md를 참조하세요.

## Observation R2 격리 연결 검사 — Phase 7B

`python scripts/check_observation_r2.py`는 기본 오프라인 검사입니다. API client/Secret/SQLite 접근 없이 SDK·Observation 계약을 검사합니다. 실제 연결은 별도 `Observation R2 Connectivity Check` Workflow의 명시적 `mode=live`와 `confirm_isolated_bucket=true`가 모두 필요합니다. 자동 schedule은 없습니다.

사용자는 새 `gwr-observation-test-<unique-suffix>` bucket과 해당 bucket만 허용하는 전용 R2 token을 만들고 GitHub Environment `observation-r2-test`에 `OBS_R2_ENDPOINT`, `OBS_R2_ACCESS_KEY_ID`, `OBS_R2_SECRET_ACCESS_KEY`, `OBS_R2_BUCKET`을 등록해야 합니다. 기존 `R2_*` fallback/.env load는 없으며 Production key/token을 재사용하지 않습니다. validate job에는 Secret이 전달되지 않습니다.

probe는 고유 `phase7b-probe/` 객체의 PUT/GET/bytes·SHA256·metadata, 두 번째 conditional PUT의412와 원본 보존, 이번 실행 객체만 정리를 검증합니다. uncertain PUT은 같은 key만 확인하고 검증 불가 시 UNCONFIRMED, cleanup 실패는 CLEANUP_WARNING으로 비정상 종료합니다. 운영 DB/Claim/Weekly Pipeline/GitHub/OpenAI/Telegram은 실행하지 않습니다. 실제 R2 인증/권한/조건부 PUT/정리는 **NOT TESTED**입니다.

설정·권한·Environment 승인·Lifecycle·token 폐기·사용자 live 실행 절차는 [Phase7B 연결 검사 안내](docs/OBSERVATION_R2_CHECK.md)를 따르세요. 이번 변경에서 bucket 생성/Secret 등록/Commit/Push/Dispatch/실제 연결을 수행하지 않았습니다.
