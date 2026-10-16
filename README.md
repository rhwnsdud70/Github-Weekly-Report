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

R2 전송은 `services/r2_persistence.py`의 명시적 API로 제공됩니다. 현재 CLI/Pipeline/Scheduler에 자동 연결하지 않았으며 GitHub Actions Workflow와 One-shot Runner도 아직 추가하지 않았습니다. 운영 DB 기본 경로와 기존 7개 테이블 구조를 그대로 사용합니다.

추가 설정은 `.env.example`의 `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`입니다. 의존성은 `python -m pip install -r requirements.txt`로 설치합니다. R2 SDK는 [Cloudflare의 boto3 설정](https://developers.cloudflare.com/r2/get-started/s3/)에 맞춰 S3 endpoint와 region `auto`를 사용합니다. Key/Secret/endpoint 또는 전체 SDK 오류를 로그로 출력하지 않습니다.

API 사용 순서:

1. `settings = Settings.load()` 후 `with R2DatabaseStore(settings) as store:` 문맥에서 `store.restore()`를 호출합니다. DB 연결을 열기 **전**, 로컬 DB가 없는 작업 디렉터리에서 수행합니다. 운영 객체는 `production/github_weekly_report.db`로 고정됩니다.
2. `restore()`는 검증한 파일을 원자적으로 게시하고 `True`를 반환합니다. `NoSuchKey` + HTTP 404인 경우에만 `False`를 반환하며 로컬 DB를 생성하지 않습니다. 그 경우 기존 `Database(settings.database_path)`로 최초 schema를 초기화합니다. `NoSuchBucket`, 인증/권한, 네트워크, 불명확한 404, 손상 DB는 예외로 중단합니다.
3. 기존 로컬 DB 또는 `-wal`/`-shm`/`-journal`이 있으면 restore를 거부합니다. 로컬 claim이나 미반영 WAL을 원격 파일로 덮어쓰지 않습니다. 자동 삭제/복구는 제공하지 않습니다.
4. 실행 완료 후 transaction을 명시적으로 commit/rollback하고 **모든 application DB 연결을 종료한 뒤** `store.upload()`를 호출합니다. 필요하면 `database.sqlite_backup.checkpoint_and_close(db.connection)`으로 WAL checkpoint와 연결 종료를 함께 수행할 수 있습니다. pending transaction은 자동 commit하지 않고 거부하며, busy checkpoint도 업로드를 차단합니다.
5. upload는 integrity/foreign-key/schema 검사를 하고 SQLite backup API로 WAL 없이 사용 가능한 단일 DB를 만듭니다. 기존 원격 운영 DB를 먼저 `backups/<UTC timestamp>-<UUID>.db`로 server-side copy한 뒤 새 DB를 업로드합니다. 백업 실패 시 운영 객체를 덮어쓰지 않습니다. 최초 운영 객체가 없는 경우에만 백업을 생략합니다. 반환값은 백업 key 또는 최초 업로드의 `None`입니다.

`R2DatabaseStore` import는 `from services.r2_persistence import R2DatabaseStore`, 설정은 `from config.settings import Settings`입니다. 전송용 임시 파일·스트림·SQLite 연결·자체 생성 SDK client는 종료 시 정리됩니다. 주입한 Mock/client의 종료 책임은 호출자에게 있습니다.

운영 객체 전송은 설정의 `DATABASE_PATH` 한 경로에만 묶입니다. test/integration/preview/fixture DB 파일명, DB 내부의 non-live run mode, fixture snapshot/metric 출처가 있으면 업로드를 거부합니다. 파일명을 production DB처럼 바꿔도 integration-test 이력이 있으면 차단하며 restore에도 동일한 내부 검사를 적용합니다. 운영 DB에 테스트 이력을 섞지 마세요.

R2 전송 자체는 여러 작업 간 분산 잠금을 제공하지 않습니다. 추후 Workflow/Runner에서 download→run→upload 전체를 단일 동시 실행으로 제한해야 기존 SQLite claim이 원격 환경에서도 유지됩니다. 이번 단계의 테스트는 실제 R2 호출 없이 `python -m pytest -q tests/test_r2_persistence.py`로 실행합니다.

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
