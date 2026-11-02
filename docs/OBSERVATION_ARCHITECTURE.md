# 경량 관측 시스템 상세 설계 v1 — 2026-10-27

상태: **Phase 1~6/7A 완료, Phase7B 격리 R2 probe 구현·Mock 검증 완료; 실제 R2 실증 NOT TESTED**. 사용자 보고로7A Actions validate 성공을 확인했으나 이번 작업에서 Dispatch하지 않았다. 7B 전용 bucket/자격증명/Environment 생성·live 검증은 사용자 작업으로 남고7C~7E는 미구현이다. 최신 계약은19절, 이전7A는18절, E2E는17절이다. 실제 서비스 API/운영 DB/기존 Claim/과거 run/Telegram/Workflow dispatch는 건드리지 않는다. 점수표/MIN_SIGNALS=2/Critical 예외/3600초는 유지한다.

## 1. 확인한 현재 구조

| 구성 | 현재 동작과 재사용 지점 |
|---|---|
| Actions/Runner | `weekly-report.yml`은 일요일19:07 KST 요청, production concurrency/queue 유지. One-shot은 R2 restore→19:00 기준 report_period→claim 사전 검사→Pipeline→SQLite finalize→R2 upload |
| Claim | One-shot이 기존 live claim을 API client 생성 전에 차단하고 Pipeline의 `claim_live_run()`도 원자적으로 중복 방지. FAILED도 재실행 금지 |
| Discovery | Trending/Search/Issue/Security 및 직전 Watchlist. ID 병합 후 대략40개. 정량 대상은 발견 순서 첫10개+Critical |
| Snapshot | 후보 전부에 metadata 배치 완료 시각을 measured_at으로 저장. 정상/처리된 실패 모두 관측을 보존하나 재발견되지 않은 저장소는 관측하지 않음 |
| Schema | `weekly_snapshots` PK=(identity,period_end). 다회 관측 이력/Trending 원본 시계열 테이블 없음. `weekly_metrics`는 지표/score JSON, failed event 본문 전체는 보존하지 않음 |
| History | `attach_history()`는 직전 주차 exact key와 ID로 조회, 현재/이전 관측이 각각 경계 이후0~3600초여야 Star/Fork 비교 |
| Metrics/Evidence | 이전 주 Issue/PR는 GraphQL 재집계로 Cold Start와 독립. activity는 하나의 신호. release/security/policy event set 및 discussion 성장 평가. Discussion 데이터 조회는 현재 TOP5 이후까지 지연 |
| R2 | `production/github_weekly_report.db`, `backups/` 전용 store. validate/export/fixture 차단 및 safe SDK client 재사용 가능. 이 store의 upload/restore를 관측 저장용으로 호출하면 안 됨 |

현재 한계는 관측 대상의 연속성과 경계 관측의 가용성이다. pool 또는 저장된 Trending이 아직 없으며 delayed run에만 현재 Star가 있어도 과거 boundary 값은 생기지 않는다. `_previous_activity()`의 오래된 주석/미사용 snapshot 변수와 달리 실제 계산은 GraphQL counts를 사용하므로 Snapshot을 Issue/PR 집계의 필수 조건으로 설계하지 않는다.

## 2. 권장 구조와 보호 경계

```text
기존 weekly run의 discovery 결과(성공/처리된 실패)
  → 순수 Pool builder → observation-pools/의 immutable manifest
                                    ↓
관측 전용 Job → Pool 읽기 → metadata/weekly Trending 관측
  → observations/의 immutable JSON 객체(실제 요청/응답 시각)
                                    ↓
weekly One-shot: 기존 R2 복원/claim 차단을 먼저 수행
  → 두 boundary의 객체를 bounded read/검증/선택 → 메모리 ObservationContext
  → 기존 후보 수집·점수·선정·TOP5 분석·전송·주간 persistence
```

관측 Job은 운영 SQLite DB를 열거나 live claim을 획득하지 않으며 OpenAI/Telegram을 import/생성하지 않는다. producer/consumer 장애가 기존 R2 DB를 교체하는 fallback은 없다. 관측값은 생산자가 쓰고 소비자는 읽기만 한다. 보고서가 실패해도 관측 객체는 독립적으로 남는다.

Pool publisher는 weekly DB를 새로 수정하지 않는다. 기존 terminal state와 R2 upload가 확정된 뒤 memory 후보로 manifest를 만든다. 실패 run에는 실제 수집한 후보만 남기며 event 자체를 만들지 않는다. R2 DB 저장 후 pool publish가 실패하면 이미 완료된 Telegram을 재발송하거나 claim을 되돌리지 않고 별도 진단한다. 두 객체 사이 분산 트랜잭션/원자성은 보장하지 않는다. 복구는 아래 읽기 전용 bootstrap으로 가능하다.

구체적인 연결 지점은 단계5에서 Pipeline이 discovery/validation 결과로 run별 로컬 PoolProposal을 준비하고, One-shot이 terminal claim·R2 upload 확인 후 게시하는 방식으로 제안한다. prepared proposal의 mode/run_id/period/hash를 현재 run과 대조하며 이전 artifact를 재사용하지 않는다. R2 upload 전 크래시나 예상 밖 실패라면 게시하지 않는다. 강제 종료로 proposal이 유실돼도 기존 DB/Claim을 되돌리지 않고 다음 관측의 읽기 전용 bootstrap으로 가능한 명단만 복원한다.

## 3. 관측 Pool 계약

초기 상수는 `config/observation_rules.py`에 구현했다. production scoring rules와 분리하며 API/R2 예산의 실제 집행은 후속 Job/store 구현 사항이다.

| 항목 | v1 제안 |
|---|---|
| 최대 크기 | `POOL_LIMIT=40`개의 고유 GitHub numeric Repository ID |
| 입력 기간 | 최근4개 완료 주차(최대28일), 미래 행/현재 평가 중인 미완료 period 제외 |
| 탐색 여유 | 이번 지시의 우선순위1~6에 따라 상위 후보 이후 남은 자리에서 최대8개. 탐색8자리 보장은 하지 않음 |
| 일반 TTL | 마지막 실제 유의미한 신호/명시적 Watchlist/소개 이후28일; 단순 metadata 재조회는 TTL 갱신 안 함 |
| 낮은 우선순위 | 14일간 새 의미 있는 근거가 없으면 deprioritize. 28일 초과는 퇴출 |
| 신규 탐색 TTL | 근거 없는 새 discovery 항목은 7일 탐색 후 신호가 없으면 퇴출. 과거 Star는 계속 unknown |
| 관측 실패 | 1회 네트워크/rate-limit 오류로 퇴출 안 함. 명시적404/410을 연속3회 확인하면 관측 중지; 403은 삭제 판정 불가 |

입력은 직전 발견 후보, Watchlist, 최근 TOP5, 최근4주 유의미한 후보/관측 pool이다. ID가 없으면 alias 대기 항목으로만 기록하고 production pool에는 넣지 않는다. rename/transfer는 ID 유지, 최신 확인된 full_name/canonical URL을 갱신한다. URL/이름은 ID 대체나 충돌 병합의 근거로 추측하지 않는다.

결정적 구성 알고리즘:

1. relevance/excluded_reason 및 실제 출처·시각을 검사한다. 현재 공식 Security 예외는 기존 검증에 위임한다. Total Star만으로 정렬/추가하지 않는다.
2. ID로 합치고 admission_reasons/source_week/evidence refs를 union한다. 동일 repo의 여러 사건을 여러 slot으로 만들지 않는다.
3. 알려진 유효 Critical, 직전 Watchlist(최대3), 최근2주 TOP5 순으로 보호 우선순위를 부여한다. 최근 공식 사건과 기존 독립 신호가 있는 후보, 최근 재관측 가치 후보가 뒤를 잇는다. 동일 등급은 last_meaningful_at 최신순, 미관측/최근 실패의 대기 오래된 순, ID 오름차순으로 고정한다.
4. Critical→Watchlist→최근TOP5→유의미한 사건/신호→재관측 가치→신규 탐색 순으로 채운다. 신규 탐색은 남은 자리에서 최대8개이며 상위 후보를 밀어내지 않는다. 기존 설계의 탐색 예약 표현보다 이번 사용자 지시의1~6 순서를 우선한 명확화다.
5. 최대40, 부족하면 그대로 둔다. Critical만40개를 넘으면 `CRITICAL_POOL_OVERFLOW`를 기록하고 entries를 비워 임의 일부 선정을 중지한다. 이 관측 pool 제한은 weekly Security discovery/필수 TOP5 규칙을 제한하지 않는다.

Pool manifest에는 ID, canonical URL/full_name, relevance 근거, first_seen_at/last_seen_at/last_meaningful_at, admission_reasons, source_period, verified event refs, expiry, 실제 관측 실패 이력과 producer run 참조를 담는다. 이전 score/grade는 **pool 조회 순서 참고용**이고 이번 주 evidence가 아니다. 현재 사건이라고 재사용하지 않는다.

최근4주 각각에서 cutoff 이전에 게시된 최신 manifest 하나만 사용한다(R2 LastModified 내림차순, 동률 key 사전순). 그 객체가 손상됐으면 해당 주를 unavailable로 남기며 제한 없이 이전 개정본을 탐색하지 않는다. 서로 다른 주의 유효 항목은 ID로 합친다. manifest에 든 미래 last_seen/source_period는 거부한다. 한 주 목록에8개를 넘는 개정본 또는 잘린 page가 있으면 그 주의 완전한 최신 pool을 확인하지 못한 것으로 처리한다.

실패 주차: discovery와 metadata가 실제 확보된 후보는 등급이 낮아도 탐색 자격을 가진다. 아직 조회하지 않은 repository나 LLM 추정은 넣지 않는다. 현재 FAILED DB만으로 복원하면 weekly_snapshots/metrics의 실측 정보와 기존 history/watchlist를 사용한다. 실패 run에서 잃은 event 제목/URL/시각을 score JSON으로 복원하지 않는다.

처음 manifest가 없는 경우: 사전 승인된 bootstrap 도구가 R2 운영 객체를 **별도 임시 경로에 다운로드하여 validate 후 read-only SQLite URI로 조회**한다. `Database()` 생성자(schema 실행), `R2DatabaseStore.restore()/upload()`, live claim 변경은 금지한다. 기존 로컬 운영 DB를 열지 않는다. 권한/네트워크 오류는 빈 pool로 위장하지 않는다. DB도 pool도 없으면 `POOL_UNAVAILABLE`, 임의 인기 repo 채우기 금지. 이 bootstrap은 후속 구현이며 이번에 실행하지 않는다.

## 4. 경계 관측 Job

기본은 방안 A. 제안 요청 시각은 일요일19:01 KST, report는 기존19:07 유지. 실제 Workflow는 7단계 승인 전 만들지 않는다. 19:01 요청도 늦을 수 있고 report보다 먼저 끝난다는 보장은 없다. 기다리기/무한 polling으로 report를 붙잡지 않는다. v1은 한 관측 invocation, 자동 주중 스케줄/재관측 loop 없음.

Job은 실제 시작 기준 `report_period(start).end`를 관측 boundary로 정한다. 늦게 실행돼도 지난 주차 값을 재현하지 않는다. 다음 주까지 지연되면 실제 최신 boundary를 기록하며 누락 주차는 누락으로 남긴다. 수동 과거 boundary backfill 옵션은 두지 않는다.

Pool은 시작 시 고정하고 기존 GitHubClient cache/rate headers/budget 사용, `/repositories/{github_id}` 방식의 metadata 조회 메서드를 재사용/얇게 확장한다. ID 직접 경로는 현재 이름 기반 repository()와 분리해야 하며 실제 구현 전 Mock 계약을 추가한다. API 응답 ID가 요청 ID와 다르면 reject한다. 본문 README/Issue/PR/Release/Discussion 수집, Search, GraphQL, LLM은 이 Job에서 하지 않는다.

각 요청마다 request_started_at/response_received_at을 UTC aware로 기록한다. **두 시각 모두** 해당 boundary 후0~3600초여야 비교에 쓸 수 있다. 네트워크가 경계를 넘어 완료한 응답도 허용하지 않는다. response_received_at을 observed_at으로 사용하며 GitHub updated_at/pushed_at/HTTP Date를 관측 시각으로 바꾸지 않는다. 후속 표준 시간 동기화가 불확실하면 unknown, 시각 보정으로 window를 늘리지 않는다.

평가용 metadata에는 ID/full_name/canonical HTML URL, stars/forks 외에 description/topics/language/created_at/pushed_at/archived/disabled 등의 실제 응답 필드를 제한된 길이로 보존한다. 이는 relevance/저장소 나이/표시에 필요한 최소 정보이며 README나 event 본문 수집이 아니다. 아래 JSON은 구조 축약 예시이며 실제 validator의 필수 metadata 집합은 단계2에서 고정한다.

Trending은 기존 weekly HTML parser를 재사용한다. source URL/페이지 범위(`since=weekly`)/실제 fetch interval/원본 byte SHA-256 및 순위 나열을 보존한다. pool metadata가 같은 관측에서 확인한 full_name→ID mapping에 대해서만 numeric ID를 연결한다. 미해결 이름은 `github_id=null`, `status=unresolved_identity`로 discovery 참고만 가능하며 rank/score 주입 금지. 별도 이름 해석 요청으로 metadata40회 상한을 넘기지 않는다. 새 pool 등록이 필요하면 다음 report의 실제 ID 조회 뒤 반영한다.

성공/부분/실패는 두 축으로 구분한다.

| 축 | 정의 |
|---|---|
| collection_status | SUCCESS=계획한 metadata/Trending 모두 검증 성공; PARTIAL_SUCCESS=일부만; FAILED=사용 가능한 source가 없음 |
| boundary_coverage | FULL/PARTIAL/NONE: 시각·ID·내용 검증을 통과한 실제 boundary 자료 비율. 늦게 전부 수집해도 SUCCESS/NONE 가능 |
| publication_status | VERIFIED/UNCONFIRMED/FAILED. GET/크기/hash 검증 전 publication 성공 아님 |

Trend fetch 실패는 metadata를 버릴 이유가 아니다. metadata 실패도 null+unavailable로 보존하고 0으로 대체하지 않는다. timeout/rate limit 이후 즉시 반복하지 않으며 remaining reserve를 유지한다. 관측 Job 실패는 보고서 성공/FAILED claim을 변경하지 않는다. 제안 종료 코드0=검증된 full coverage,2=부분/시각상 부분,1=관측/게시 실패. 정확한 코드·운영 알림은 구현 전 확정한다.

## 5. R2 저장·무결성·멱등성

기존 Bucket과 R2 client factory/설정/safe category를 재사용하되 새 `R2ObservationStore`를 만든다. 운영 DB store를 subclass하여 key만 바꾸지 않는다. `production/`와 `backups/` 쓰기 기능을 제공하지 않는 별도 API로 제한한다.

```text
observations/v1/production/YYYY-MM-DD/<observation-id>.json
observation-pools/v1/production/YYYY-MM-DD/<pool-id>.json
```

날짜는 **KST boundary 날짜**이며 실제 observed_at은 payload에 별도로 보존한다. `test-observations/`, `test-observation-pools/`는 fixture 전용 별도 prefix이다. 경로는 검증된 enum/날짜/UUID만으로 생성하며 caller의 임의 key를 받지 않는다. payload의 mode/fixture 플래그와 key namespace를 모두 검사한다. 파일명만 바꿔 production에 올리는 것을 허용하지 않는다.

제안 observation envelope(수치는 설명용 합성 예시, 실제 업로드 불가):

```json
{
  "schema_version": 1,
  "payload": {
    "mode": "design-fixture",
    "fixture": true,
    "observation_id": "00000000-0000-4000-8000-000000000001",
    "boundary_end": "2026-11-01T10:00:00Z",
    "period_start": "2026-10-25T10:00:00Z",
    "started_at": "2026-11-01T10:01:00Z",
    "completed_at": "2026-11-01T10:01:10Z",
    "pool_ref": {"key": "test-observation-pools/example.json", "sha256": "EXAMPLE_ONLY"},
    "collection_status": "SUCCESS",
    "repositories": [{
      "github_id": 900001,
      "full_name": "synthetic-fixture/repo",
      "source_type": "github_repository_metadata",
      "source_url": "https://api.github.com/repositories/900001",
      "request_started_at": "2026-11-01T10:01:02Z",
      "observed_at": "2026-11-01T10:01:03Z",
      "status": "confirmed",
      "stars": 1200,
      "forks": 80
    }],
    "trending": {"status": "unavailable", "entries": []}
  },
  "integrity": {"algorithm": "sha256", "payload_sha256": "COMPUTE_IN_FIXTURE_BUILDER"}
}
```

구현 시 producer repo/run ID/attempt/code version, monotonic request duration, source별 safe HTTP status, rate-limit resource/counters, Trending request/response interval/page hash, boundary_coverage, 실패 category도 필수다. Token/header/raw SDK error/서명 URL/긴 본문은 보관하지 않는다. 위 예시는 설명용으로 hash와 namespace가 유효하지 않으므로 production validator를 통과하지 않는다.

JSON은 UTF-8/최대1MiB, repo40/Trending30건, non-negative integer count(bool 제외), aware 시각, ID 중복 없음, source URL 고정 패턴, 깊이/문자열 상한을 검증한다. 미지원 schema, 중복 JSON key, NaN/Infinity, 손상, 미래 시각, 응답 ID 불일치를 거부한다. 객체에 든 임의 URL로 fetch하지 않는다.

checksum은 `json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')`의 SHA-256이다. payload만 계산하고 읽을 때 동일 규칙으로 검증한다. ETag를 파일 hash로 보지 않는다. checksum은 손상 검사이며 권한 있는 작성자의 변조를 인증하는 장치는 아니다.

작은 JSON의 단일 conditional PUT(`IfNoneMatch='*'`) 후 같은 key를 GET해 크기/hash/schema를 재검증한다. multipart/부분 JSON 추가쓰기/사후 수정은 하지 않는다. 작업 중단 시 미공개 또는 실패 항목까지 포함한 완결된 PARTIAL envelope만 공개한다. 같은 logical invocation의 ID와 bytes를 고정하여 transport 재송신에 재사용한다. 412는 GET 결과 hash가 같을 때만 멱등 성공이며 다르면 IMMUTABLE_OBJECT_CONFLICT다. timeout은 GET으로 확인하며 미확인은 UNCONFIRMED로 남긴다. 원격 삭제/강제 덮어쓰기는 없다. 새 Job rerun은 별도 실제 관측 ID이지만 consumer의 boundary/ID 중복 제거로 signal은 증가하지 않는다.

인증/권한/네트워크/NoSuchBucket/명백한 NoSuchKey는 기존 safe category로 구분한다. 성공한 빈 List와 실패한 List는 다르다. 403/모호한404를 초기 빈 저장소로 간주하지 않는다. List/Get/Put 권한을 별도 확인한다. 기존 Bucket/key 재사용은 가능하지만 prefix별 권한 격리가 보장된다고 가정하지 않는다. 고정 prefix API와 Mock으로 오염을 막고 별도 credential/Bucket은 운영 결정으로 남긴다.

## 6. 보고서 소비 계약

이 절은 **미구현 연동 설계**이며 이번에 attach_history/Pipeline을 변경하지 않는다.

v1의 Pool은 관측 대상을 유지하기 위한 목록이며 이번 주 후보를 강제로 늘리는 목록이 아니다. report의 정상 discovery로 발견한 ID를 ObservationContext에서 조회한다. Pool 전체를 최종 후보나 TOP5에 자동 삽입하지 않는다. 관측 목록을 discovery hint로도 활용할지는 기존40개/Critical 보존/새 후보 탐색 예산과 함께 별도 결정한다. 따라서 관측을 확보했어도 해당 저장소가 다시 후보가 되지 않으면 이번 보고서에는 사용되지 않을 수 있다.

1. 기존 R2 production 복원과 Claim 사전/원자적 검사를 먼저 수행한다. 기존 claim이면 관측 List/Get, GitHub/OpenAI/Telegram 호출 없이 종료한다. 관측 Job은 report claim을 사용하지 않는다.
2. 소비 시작 cutoff 시각과 object key/hash 집합을 고정한다. cutoff 전에 공개된 current/previous boundary prefix만 읽으며 이후 도착한 객체로 같은 run의 결과를 바꾸지 않는다.
3. schema/hash/namespace/production provenance/시각을 검증한다. 손상 객체는 근거로 쓰지 않고 목록 상한 초과/중간 pagination 중단을 완전한 집합으로 취급하지 않는다.
4. metadata의 request_started_at과 observed_at 모두 `B <= time <= B+3600초`, observed_at<=completed_at<=cutoff여야 한다. period는 일요일19:00 KST/168시간이다. GitHub updated_at으로 관측 시각을 대체하지 않는다. 이벤트의 `start <= event < end`와 관측 허용 규칙을 구분한다.
5. boundary/ID별 가장 이른 유효 response를 선택하고 동률은 request 시작/object ID 사전순으로 안정화한다. 시각이 다른 실측은 합산/평균하지 않는다. 동일 ID/시각/source의 값이 다르면 OBSERVATION_CONFLICT로 해당 metric을 unknown 처리한다. 충돌을 단순 우선순위로 숨기지 않는다.
6. 기존 SQLite의 유효 boundary Snapshot도 같은 선택기에 넣되 행을 변경/삭제하지 않는다. DB의 batch observed_at과 개별 관측의 정밀도 차이를 남긴다. 동일 metric의 여러 source를 독립 signal로 늘리지 않는다.
7. Star/Fork 비교는 같은 ID의 각 경계 실측으로 한다. delta=current_boundary.stars-previous_boundary.stars, growth 분모=previous_boundary.stars. 이전0은 기존 not_applicable, 누락은 unknown, 감소도 그대로 보존한다. 실측 간격/경계 offset도 남겨 관측 간격까지 정확히168시간이라고 주장하지 않는다.
8. current boundary1200/previous1000/23시 live1500이면 delta200/20%이다. 500/50%가 아니다. 늦은 Candidate의 stars=1500을 둔 채 attach_history의 observed_at만 과거로 바꾸는 우회는 금지한다. SnapshotComparison에 값/ID/실제 시각/object hash를 명시적으로 전달한다.
9. 기존 score_candidate의 T는 candidate.repository.stars를 읽는다. v1 권장안은 **평가용 Candidate를 실측 boundary metadata로 구성**하고 늦은 fresh metadata는 latest_metadata로 별도 보존하는 것이다. 평가 Star의 observed_at을 표시하고 최신 Star는 별도 라벨로 제공한다. 1200을 평가에 사용한다고 명시하며 최신1500을 몰래 덮어쓰지 않는다. T 표/score 함수는 불변이다. legacy source 선택과 출력 의미를 확정·테스트하기 전 소비를 활성화하지 않는다.
10. 신규/재발견 ID에 다른 ID의 과거 값을 주입하지 않는다. previous 없는 신규 repo는 이번 current를 저장해도 비교는 unknown, 다음 주부터 가능하다. boundary metadata가 없으면 기존 live candidate를 유지하며 관측 비교는 unknown이다.
11. Trending은 동일 scope의 가장 이른 유효 **페이지 전체 하나**를 선택한다. repo별 최고 rank를 여러 페이지에서 합치지 않는다. hash/ID 고정, 동일 관측 시각의 순위 충돌은 unknown이다. 같은 관측 Job에서 ID가 검증된 entry만 Metric.known을 만들고 미해결/지연/페이지 누락은 unknown으로 둔다. 미등장을 False로 만들지는 않는다.
12. 저장 Trending이 boundary 후 허용 범위에 실제 관측됐다면 report 본체가 몇 시간 늦어도 소비할 수 있다. 실제 rolling page 관측이며 과거19:00 값이라고 위장하지 않는다. 주 run의 clock은 실제 시각 그대로, 해당 자료만 그 관측 시각으로 검증한다. live fetch의3600초 제한은 유지한다.
13. 관측 R2 접근 실패는 그 자료를 전부 배제하고 OBSERVATION_STORE_UNAVAILABLE로 남긴다. 0/Trending false로 대체하지 않는다. 다른 정상 GitHub source의 평가는 계속할 수 있지만 근거 부족이면 기존 FAILED다. 운영 DB restore 실패는 기존처럼 전체 중단한다. 정상 빈 결과 OBSERVATION_MISSING과 구분한다.
14. 기존 score/validate/merge/ranking/critical/allow_repeat를 호출한다. 파일 존재/Pool 입장/같은 Star 중복 관측/Issue와 PR API를 새 signal로 세지 않는다. LLM의 사실·인용·actor·impact 검증과 예산은 유지한다.

ObservationContext는 메모리 typed model과 run artifact/기존 report JSON source 참조로 남긴다. 관측 소비 때문에 weekly_snapshots에 먼저 INSERT하거나 measured_at을 바꾸지 않는다. 기존 persist_measurements는 report가 실제 수집한 late metadata를 그대로 보존하고, boundary 관측의 정본은 append-only 객체로 유지한다. 평가용 Candidate와 latest_metadata를 분리하면 persistence adapter도 최신 metadata를 명시적으로 받도록 설계해야 한다. 그렇지 않으면 boundary 값을 늦은 measured_at으로 저장하는 버그가 생긴다. DB import/migration은 v1에서 하지 않는다.

## 7. 요청량·실행 시간·보관 예산

| 경로 | 제안 상한과 주의점 |
|---|---|
| 관측 GitHub | metadata logical40건, redirect/retry 포함 REST HTTP attempt45회 이하. Search0/GraphQL0. 기존 remaining reserve5 유지. 성공40건 보장은 아님 |
| 관측 Trending | HTML1 logical fetch/최대2 attempts. 즉시 반복하지 않고 window를 넘긴 응답은 기록만 |
| Job 시간 | timeout10분 제안. request timeout/중단을 포함해 부분 결과를 완결화. 시작 자체가 늦으면 coverage NONE |
| Pool R2 읽기 | 최근4주 prefix의 List 각1page(100개 제한), manifest GET 합계4개 이하. 잘린 prefix는 완전한 최신 pool로 간주하지 않음 |
| 관측 게시 | PUT1+검증GET1. retry는 별도 합계6attempts 제한. Pool 읽기 포함 정상 요청은 최대10회, bootstrap DB GET은 명시적 별도 작업 |
| report 관측 읽기 | 두 경계의 List 각1page+각8objects GET=정상 최대18회. 9개 이상/잘린 page/예산 초과는 불완전 자료로 표시하고 관측을 배제 |
| Pool 게시 | terminal 후PUT1+GET1. GitHub/LLM/Telegram 호출 추가 없음 |
| report 기존 예산 | candidate70/validation25/deep80과 Search reserve/GraphQL 계측 유지. logical과 실제 SDK 재시도 횟수 차이는 단계3에서 따로 상한 검증 |

기존 R2 client의 SDK 재시도도 물리 attempt 계측에 포함한다. 관측 Job의 Pool읽기+게시 총 R2 attempt는30회, report 관측 읽기는54회, Pool 게시만은6회 상한을 제안한다. 정상10/18/2회의 최대3배라는 장애 상한이지 추가 호출을 항상 소비한다는 뜻은 아니다. 제한 도달은 미확인 처리한다. GitHub metadata 요청 timeout10초, 내부 작업 deadline8분/Workflow timeout10분을 제안하며 중단·직렬화·게시 여유를 남긴다. 강제 종료 전에 반드시 게시된다고 보장하지는 않는다.

주당 observation1+pool1이면 연 약104objects, boundary별 각8개 개정본 상한이면 연 약832objects이다. 각 객체1MiB 상한을 모두 채우면 연 약832MiB라는 보수적 가정이다. 실제 요금/무료 구간은 추정하지 않으며 GET/List 등의 요청 비용도 별도다. 이번에 lifecycle/삭제를 설정하지 않는다.

retention 제안은 Pool90일/관측180일과 마지막 유효 경계2세대 보호다. 감사 참조가 더 오래 필요하면 hash-only/원본 장기 보관을 별도 결정한다. 초기에는 삭제 없이 용량을 관찰하며, 장기 용량이 무한히 제한된다고 주장하지 않는다. 자동 정리는 참조 검사 후 별도 승인 작업이다.

## 8. 독립 근거 조회 순서 개선안

관측 v1에 Release/Discussion/API 본문을 추가하지 않는다. 별도 shortlist 순수 함수를 통해 발견 순서 첫10개 고정을 개선한다. Critical 전부→이미 확인된 period 내 공식 release/security/policy→유효 Star/Trending과 관련성→Watchlist/신규 탐색 순을 검토한다. 일반10개+Critical/validation budget을 유지하고 동률은 ID로 고정한다. 최종 ranking을 바꾸는 작업이 아니라 조회 순서이며 전주 점수나 총Star만으로 정렬하지 않는다.

지금 Release는 shortlist 뒤에 많이 수집되므로 Release 우선순위만 바꾸어 미수집 Release를 알 수는 없다. 이미 확보한 events만 활용하고 아직 보지 않은 사건을 추정하지 않는다. 예산 내 탐색/단계적 조회가 필요한지 별도 Mock으로 측정한다. 후보가 부족해도 gate를 낮추지 않는다.

Discussion 검증 전 최대3후보에 한정한 GraphQL 활동 metadata 실험은 별도 변경이다. 이전 total이 없으면 성장률을 만들지 않으며 단순 누적 total만으로 활발하다고 하지 않는다. 기존 optional 요청을 줄여 예산 안에서 가능할 때만 적용하고 본문 심층 분석은 TOP5에 남긴다. 새 독립 signal/점수 정의가 필요하면 별도 결정으로 돌린다.

## 9. 오프라인 Fixture와 테스트 계획

`tests/fixtures/observation_design_time_cases.json`과 `test_observation_design_contract.py`는 기존 시간 계약을 유지한다. Phase1의 `test_observation_pool.py` 및 Phase2의 `test_observation_validation.py`가 추가됐다. 아래 표에서 모델/Pool 수준의 항목은 구현·검증했고, 실제 store/producer/consumer의 실패 주입 및 end-to-end 연결은 Phase3~7 수용 계획이다.

| 상황 | 기대 결과 | 검증 단계 |
|---|---|---|
| 정상/10분/3600초 일치 | current/previous 유효하면 비교 허용 | 이번 시간 계약, 단계2/5 |
| 요청은 창 안, 응답은3600초 초과 | boundary 자료 배제/과거시각 보정 금지 | 이번 시간 계약, 단계4 |
| 1시간 초과/경계 직전/미래시각 | window 밖은 배제, cutoff 내용 검증 추가 | 이번 window 계약, 단계2/5 |
| 관측 작업 누락/빈 List | MISSING+unknown, 다른 사건 근거는 평가 | 단계3/5/6 |
| 신규 ID/previous 없음 | current 보존/delta·growth unknown | 이번 시간 계약, 단계1/5 |
| 같은 ID 여러 시각/동시각 동일값/상이값 | earliest/중복 배제/CONFLICT unknown | 단계2/5 |
| PUT timeout/412/검증 실패 | 동일bytes 확인만 멱등 성공, 미확인은 UNCONFIRMED | 단계3 |
| JSON 손상/hash 변경/중복key/크기 초과/fixture 위장 | reject, raw body·Secret 비출력, DB 불변 | 단계2/3 |
| 기존 SUCCESS/FAILED/RUNNING claim | 관측List/API/send 없음, 기존claim 불변 | 단계5/6 및 기존Mock |
| 관측 게시 후 보고서 실패 | 객체 불변/FAILED 보존/다음주 유효 관측만 소비 | 단계6 |
| Trending 없이activity+Major Release | 기존 기준으로5개 선정, 중복source 신호 증가 없음 | 기존test_observation_delay 및 단계6 |
| R2 인증/네트워크/불완전 List | 빈 자료로 위장하지 않고 fail-closed | 단계3/5 |
| report4시간 지연, 두 경계 관측은 정상 | 관측 시각으로 비교/late Star 혼합 금지 | 이번 시간 계약, 단계5/6 |
| legacy DB와 저장 관측 충돌 | 선택 규칙/CONFLICT, 행·시각 변경 없음 | 단계5 |
| 시험 namespace/가짜ID/허위 Trending | production 거부, Mock만 사용 | 단계2/3/6 |

후속 fixture는 `tests/fixtures/observations/`에 둔다. FakeS3는 주입식이며 실 SDK 연결을 차단한다. 단계6은 기존 DemoBackend/임시 SQLite/FakeS3로 전체 경로를 연결해 실제 API/LLM/Telegram 발송 없이 TOP5·persistence를 검증한다.

## 10. 단계별 구현 계획

Phase1~3 파일은 생성·검증 완료이며 Phase4~7 파일은 아직 예정이다. Phase3은 저장 API와 Mock 검증 완료이고 실제 R2 운영 연결은 하지 않았다. 전체 관측 Job/소비 경로 구현 완료를 의미하지 않는다.

| 단계 | 예정 파일과 선행조건 | 회귀 위험 | 수용 기준 |
|---|---|---|---|
| 1 Pool | config/observation_rules.py, services/observation_pool.py, tests/test_observation_pool.py. 상한/TTL 합의 | 인기순 고정/TTL 연장/fixture·ID 혼입 | 순수 builder/40 이하/ID 유일/결정적 순서/실패후보·탐색·만료·overflow 검증/DB 불변 |
| 2 모델 | models/observation.py, services/observation_validation.py, fixture/fuzz tests. 1 및schema/hash 확정 | 시간·누락·0 혼동/hash 오류 | interval/period/ID/source/크기/hash/fixture 검증/실측만 승인 |
| 3 R2 store | services/r2_observation_store.py, tests/test_r2_observations.py. 2 및prefix·권한 결정 | 운영key 덮어쓰기/부분공개/List 예산 | conditional PUT/Get/List Mock, timeout·412·손상·인증 분류, production/backups 쓰기0, 기존R2 회귀 |
| 4 관측 Job | services/observation_job.py, scripts/run_boundary_observation_once.py, tests. 1~3/bootstrap/budget 결정 | window 위장/Actions 지연/API 과다 | metadata40/attempt45/Trending2 이하/실측 시각/부분공개, DB·claim·LLM·send0 |
| 5 소비 | services/observation_consumer.py, history/metric/pipeline/runner/formatter/persistence의 한정 adapter, tests. 2~4 및Star표시·충돌 규칙 합의 | late/live 혼합/claim 전 호출/이중신호/잘못된Snapshot | claim 우선/typed context/T와값·시각 일치/누락·충돌 fail-closed/score·grade·기준 불변/latest 보존 |
| 6 Offline 통합 | tests/test_observation_integration.py 및FakeS3. 1~5 수용 | 보고서 실패·pool 게시누락/운영DB 혼입/중복send | 전체실패fixture/다음주비교/DB hash·prefix 보호/전체pytest·compile·diff check |
| 7 배포·운영 | 관측 Workflow/소비 설정/README/status. 1~6 및별도 사용자 승인 | 권한/Job 순서/지연/실데이터 | offline validate→관측1회만 검증→이후 신규period 소비. 기존FAILED 재실행 금지 |

단계5까지 default OFF, production에 fixture/임의과거period/force flag를 추가하지 않는다. rollback은 consumer OFF이며 DB/Claim/object 삭제가 아니다. 관측을 도입해도 TOP5가 항상 생성된다고 보장하지 않는다.

## 11. 별도 결정 사항

1. Pool40/탐색8/TTL7·14·28일/Critical overflow 처리의 운영 합의.
2. 관측19:01/report19:07 제안과 동일 Actions 지연 위험, 필요한 경우 외부 정시 실행원 사용 여부. 이번에 새 기반을 계약하지 않는다.
3. 공통 credential/Bucket 재사용 또는 관측 전용 credential/Bucket. prefix만으로 IAM 격리를 가정하지 않는다.
4. cutoff/earliest/동시각 충돌/legacy source 선택 규칙. 최고 Star/최고 rank 취사선택 금지.
5. boundary 평가 Star와 latest Star의 표시 및 T 입력 시점. 소비 단계의 명시적 수용 게이트다.
6. 90/180일 retention과 감사 보관, 초기 무삭제 기간의 용량 모니터링 및 정리 작업 시점.
7. Pool 게시 경고와 weekly terminal 상태의 구분, 관측 PARTIAL/NONE/UNCONFIRMED의 알림. 기존 전송 성공을 위장하지 않는다.
8. shortlist/Discussion 실험은 관측v1과 별도 변경으로 분리하고 어떤 optional 요청 예산을 대체할지 합의한다.

Phase1~3 구현은 각 사용자 지시로 승인되어 완료했다. Job·소비·배포나 실환경 활성화 승인은 아니며, 후속 구현 범위를 지정받은 뒤 Phase4부터 진행한다.

## 12. Phase 1~2 구현 계약

이 절의 범위는 이번 Phase1~2 사용자 승인으로 구현했다. 11절의 운용·배포 관련 결정은 여전히 후속 작업이다.

### Pool API

- `PoolRecord`: 확인된 입력을 명시적으로 `verified=True`로 전달하는 frozen DTO. numeric ID, 실제 source_period_end, first/last seen, meaningful/critical/watchlist/report 시각, 과거 signal/event 참조, 이전 실패 상태를 보존한다. 불완전 RUNNING 입력과 unverified 입력은 선정하지 않는다.
- `build_pool(records, as_of=..., fixture=False) -> PoolResult`: 네트워크/DB/시스템 clock 없는 순수 함수다. 이전 `PoolEntry`도 재입력할 수 있지만 기존 TTL anchor를 갱신하지 않는다. Pool 입력의 aware 시각은 UTC로 정규화한다. 미래/미완료 source period는 거부하거나 lookback 밖으로 제외한다.
- 같은 ID는 최신 실측 metadata를 기준으로 병합하고 이름 변경을 추적한다. 동시각 tie는 이름/source_kind/정규화 record 표현으로 결정적으로 해소한다. 최신 relevance 거부를 과거 positive 항목으로 덮지 않는다.
- Critical 우선순위에는 security 신호와 GitHub 전역/저장소 advisory 참조를 요구한다. 과거 Critical이7일을 넘으면 현재 Critical 보호로 취급하지 않는다. 관련성 자체가 없으면 선정하지 않고, exclusion 예외는 활성 Critical에만 적용한다.
- 최근4개 완료 boundary를 조회 범위로 삼는다. 7일/28일 TTL 끝 시각은 포함, 초과 시 만료하며 14일 시점부터 하향한다. metadata 재조회로 의미 있는 사건의 TTL을 연장하지 않는다. 최근 Watchlist는7일, TOP5는14일 기준이다.
- `pool_manifest(result, as_of=..., fixture=False)`는 detached JSON-ready **로컬 proposal**만 만든다. result의 모드/as_of를 재명명하지 못한다. R2용 Pool envelope/UUID/hash/외부 저장 검증은 Phase3의 별도 계약이며 observation envelope와 혼용하지 않는다.
- 이전 signal/event는 admission 참고 자료이며 Candidate.metrics, 현재 Evidence 또는 score를 만들지 않는다. verified 입력의 실제 출처를 확인할 adapter는 후속 단계이며 이 DTO가 GitHub 접근을 증명하지는 않는다.

### 모델·무결성 API

- `models/observation.py`: frozen Producer/PoolReference/SafeDiagnostics/RepositoryMetadata/RepositoryObservation/TrendingEntry/TrendingObservation/ObservationPayload/Integrity/ObservationEnvelope/ObservationValidation을 제공한다.
- wire mode는 `production-observation`/`fixture-observation`, 검증기 namespace 인수는 `production`/`fixture`다. fixture 플래그, mode, Pool prefix를 모두 일치시킨다. 실제 observation R2 key와의 결합 검사는 store를 구현할 때 추가한다. 모든 표식을 제거한 허위 자료를 모델만으로 진짜라고 증명할 수는 없으며 Phase3~4의 신뢰된 producer/저장 접근 통제가 필요하다.
- 생성은 `seal_observation(payload)`→`encode_observation(envelope)` 순서다. seal은 hash 계산만 하므로 **신뢰하기 전에 반드시 validate_observation을 호출**한다. validator는 모델/dict/JSON str/bytes를 받고 `now=UTC aware cutoff`, 고정 namespace를 명시해야 한다.
- timestamp는 UTC aware만 허용하며 KST/naive는 거부한다(legacy history API의 정책은 변경하지 않음). UTC `Z`/`+00:00` 등 동등한 표현은 먼저 동일한 Z 형태로 바꾸고 설계의 compact/sorted UTF-8 canonical JSON을 계산한다. JSON 공백/key 순서/escape 표현은 hash에 영향이 없고 배열 순서와 실제 본문 내용은 보존한다. 모든 필드가 integer/string/bool/null/구조 타입이므로 float/NaN/Infinity는 허용하지 않는다.
- 전체 wire JSON1MiB, 깊이16/node10000, repo40/Trending30, text512자 상한을 적용한다. 미지 필드(Authorization/raw body/error_message 포함), 중복 key, 잘못된 URL/ID/schema/hash를 거부하며 고정 category만 오류로 노출한다.
- confirmed metadata에는 ID/full_name/정확한 GitHub URL/created_at, description/topics/language/pushed_at/archived/disabled와 별도 실측 stars/forks를 담는다. unavailable은 metadata/count를 null로 유지한다. 이 모델은 이전 Snapshot 필드를 요구하거나 생성하지 않는다.
- Source 시각은 job interval 안이어야 하고 observed_at=response_received_at을 강제한다. 역전/미래/비UTC/관측 시각만 재작성한 자료는 거부한다. request/response가 있는 source의 monotonic duration_ms와 벽시계 간격 차이는 integer millisecond의 양자화 여유1ms까지만 허용한다. 이는 3600초 적격 창을 늘리지 않는다. clock의 진위·원격 실측 자체에 대한 외부 인증은 아니다.
- 완전한 collection_status(SUCCESS/PARTIAL_SUCCESS/FAILED)와 boundary_coverage(FULL/PARTIAL/NONE)는 각 source의 확인/시간/ID 상태에서 다시 계산해 주어진 값과 대조한다. 늦거나 경계 직전의 정직한 기록은 성공 수집이면서 NONE으로 유효할 수 있다. 삭제/시각 수정 없이 eligible ID/reason만 반환한다.
- publication_status는 VERIFIED/UNCONFIRMED/FAILED를 기록할 수 있으나 모델 검증은 원격 게시를 증명하지 않는다. 불변 객체의 seal 뒤 필드를 바꾸면 hash가 달라진다. Phase3에서는 초기 객체의 UNCONFIRMED 상태를 수정해 덮어쓰지 말고, 별도 검증 receipt/반환 상태로 VERIFIED를 나타내는 계약을 확정해야 한다.
- Trending resolved ID는 같은 envelope의 실제 confirmed metadata와 full_name이 일치해야 한다. 미해결 이름은 기록하지만 score용 eligible로 반환하지 않는다. 페이지 rank는1부터 연속/중복 없이 검증한다. 유효 페이지 전체 선택과 서로 다른 객체의 충돌 해소는 Phase5다.

### 구현/검증 경계

Phase1~2의 순수 구현은 Pool/모델 및 한 객체 내부 검증을 수행한다. 후속 Phase3에서 conditional PUT/GET/List와 중단 처리를 구현했다(13절). 여러 객체의 의미상 earliest/충돌 선택, 실제 Job, 기존 Pipeline 소비, SQLite persistence adapter, 새 Workflow는 **미구현**이다. Production이 새 모듈을 import하거나 호출하도록 연결하지 않았다. 기존 R2 Backup/Persistence와 Workflow는 수정하지 않았다.

실행 테스트: `python -m pytest -q tests/test_observation_pool.py tests/test_observation_validation.py` 및 전체 pytest. 네트워크/DB/R2/GitHub/OpenAI/Telegram 생성자를 차단한 격리 테스트도 포함한다. Pool envelope/fixed key/Receipt/SDK 예산은 후속 Phase3에서 13절과 같이 확정했다. 실제 운영 credential 권한과 서버 호환성은 아직 실증하지 않았다.

## 13. Phase 3 구현 계약 — 2026-10-28

구현 파일은 `services/r2_observation_store.py`, `services/observation_pool_validation.py`, `models/observation_storage.py`이며 observation_rules에 저장 예산 상수를 추가했다. R2 SQLite store를 상속하지 않고 기존 create_r2_client의 안전한 설정/SDK 로그 억제와 safe error 분류만 재사용한다. SQLite helper/production 파일은 수정하지 않았다.

### Public Interface

`R2ObservationStore(settings, *, as_of, namespace='production', profile='publication', client=None)`를 사용한다. as_of/boundary는 UTC aware이며 Store가 임의의 과거 clock을 생성하지 않는다. 고정 cutoff의 단일 직렬 작업 context이고 thread-safe 공유 client 용도가 아니다. context manager/close를 제공하며 주입 client는 닫지 않고 해당 Store의 이벤트 hook만 제거한다. 자체 생성 client만 닫는다.

| 메서드 | 계약 |
|---|---|
| publish_observation(envelope) | Phase2 검증→canonical bytes→현재 boundary key→conditional PUT→실제 GET 검증→Receipt |
| publish_pool(envelope) | 새 Pool envelope/Phase1 재계산 검증 후 같은 게시 경로 |
| get_observation(boundary, observation_id) | 현재/직전 boundary만, 검증된 StoredObject 또는 명백한 NoSuchKey+404이면 None |
| get_pool(boundary, pool_id) | 최근4개 완료 boundary만, 검증된 StoredObject 또는 명백한 key 없음 |
| list_observations(boundary) | 현재/직전 boundary의 완전한 단일 제한 page, 검증된 ObjectReference tuple |
| list_recent_pools() | 최근4개 완료 주차별 제한 page, 최대32개 reference만 반환. 내용을 모두 GET하거나 소비 후보를 선정하지 않음 |

caller가 raw Key/Prefix를 주는 public 메서드는 없다. namespace/kind/boundary/UUID에서 key를 생성하고 내부 transport에도 허용 prefix 이외의 요청을 막는 guard를 둔다. 관측 production key는 `observations/v1/production/<KST경계날짜>/<UUID>.json`, Pool은 `observation-pools/v1/production/...`다. fixture는 `test-observations/v1/...`, `test-observation-pools/v1/...`만 쓴다. `production/`/`backups/`에는 읽기·쓰기·copy·delete를 하지 않는다.

기존 report_period()의 반환은 KST이지만 wire 계약은 UTC-only라 새 Store/seal_pool 내부에서 **동일한 경계 instant를 UTC로 변환**한다. 기존 보고 기간 계산이나 관측 시각을 변경하지 않는다.

### Pool envelope 확정

Phase1 `pool_manifest` proposal은 그대로 유지하고 `PoolPayload(pool_id, mode, fixture, period_start, boundary_end, created_at, producer, manifest, publication_status)`로 감싼다. `PoolEnvelope`는 기존과 같은 schema_version/payload/integrity 구조다. manifest의 as_of 시점이 period에 맞고 created_at/cutoff보다 미래가 아닌지 검사한다.

`seal_pool(result, pool_id=..., created_at=..., producer=...)`, `encode_pool(envelope)`, `validate_pool(value, now=..., namespace=...)`를 제공한다. UTC Z 정규화 후 compact/sorted canonical payload SHA-256을 사용한다. Pool timestamp 필드까지 정규화하며 원본 입력은 변경하지 않는다. 전체1MiB/40개/ID중복/enum/fixture/producer/source period/미래 시각을 검사한다.

각 entry의 record로 Phase1 builder를 다시 계산해 ID 집합/순서/priority/expiry/deprioritized/admission reasons가 일치하는지 확인한다. 과거 source_kinds는 enum/중복/현재 source 포함을 검증하되 이를 새 independent evidence로 만들지 않는다. 제외됐던 원본 입력을 envelope만으로 재현할 수 없으므로 diagnostic은 안전한 category/비음수 정수 구조만 확인한다. Critical overflow는 빈 entries와 명시적 overflow 진단을 보존한다. 이 검증이 출처 자체에 대한 외부 인증을 제공하는 것은 아니다.

### 게시·Receipt·실패 처리

입력 envelope의 publication_status는 **UNCONFIRMED만** 허용한다. 저장 뒤 이 필드를 VERIFIED로 바꾸어 객체를 덮어쓰지 않는다. 별도 frozen PublicationReceipt에 object_key/object_id/payload SHA/schema/실제 byte_size/boundary/status/verification_result/safe error category와 HTTP status를 담는다. byte_size는 VERIFIED일 때만 검증된 크기이며, 미확인 Receipt에는 None이다.

- PUT은 IfNoneMatch='*'로 한 번 호출한다. 사전 HEAD→무조건 PUT 방식이 아니며 Store 자체의 PUT retry loop가 없다. SDK retry에도 같은 ID/bytes/condition을 유지한다.
- PUT timeout/네트워크 오류/5xx/412는 동일 key를 GET해 확인한다. 같은 canonical bytes+schema+SHA이면 VERIFIED, 다른 valid content이면 IMMUTABLE_OBJECT_CONFLICT다. 412 후 손상된 기존 자료도 교체하지 않고 충돌로 분류한다.
- PUT 인증/권한/rate-limit/명백한 요청 오류는 안전한 오류로 중단한다. GET timeout/접근 거부/예산 부족 등으로 확인할 수 없거나 PUT 후 객체가 안 보이면 UNCONFIRMED이며 성공이 아니다.
- PUT 정상 응답 후 JSON/hash/크기/key/payload 불일치가 발견되면 검증 오류로 중단한다. JSON 원문·SDK 원문은 출력하지 않고 객체를 삭제/수정하지 않는다.
- Store가 소유하는 객체는 canonical bytes여야 한다. hash가 같아도 비canonical remote 표현은 NON_CANONICAL_STORED_OBJECT로 거부하며 임의 정리/덮어쓰기를 하지 않는다. 새 게시의 동일 key/상이한 bytes는 conflict다.
- 스트림은 크기 상한 내에서 끝까지 읽고 항상 닫는다. ContentLength/실제 byte 수를 검사한다. public GET/List는 LastModified가 cutoff 이후인 자료를 채택하지 않는다. 게시 확인 GET은 방금 PUT된 객체이므로 LastModified cutoff만 예외로 하되 payload의 관측/생성 시각 검증은 유지한다.
- R2ObservationError는 고정 category/HTTP status만 제공한다. HTTP401/403은 AUTHENTICATION_OR_PERMISSION,429는 RATE_LIMIT, 명백한 bucket 없음은 BUCKET_NOT_FOUND, transport는 NETWORK_ERROR다. NoSuchKey+404만 None이며 모호한404/NoSuchBucket/인증 실패를 빈 저장소로 취급하지 않는다.

### List/GET과 요청 예산

page마다 MaxKeys=9(8개+초과 판별용1개)를 요청한다. IsTruncated=true는 INCOMPLETE_LIST로 중단하고 continuation을 무한 추적하지 않는다. 9개 이상은 OBJECT_LIMIT, prefix/UUID 오염·중복 key·잘못된 response count는 오류다. 이는 기존 설계의 100개 제한 page보다 보수적인 구현이며 logical List 횟수는 그대로다. 빈 결과와 실패를 구분하고 부분 tuple을 정상 반환하지 않는다.

관측 GET은 boundary당8회, Pool GET은 총4회 상한이다. 목록은 metadata reference만 반환하며 earliest 선택/의미상 충돌 처리는 Phase5에 남긴다. Pool 최신 객체 선정도 Store가 대신하지 않는다.

| profile | logical SDK call 상한 | before-send 시도 상한 | 정상 용도 |
|---|---|---|---|
| publication | 2 | 6 | PUT1+GET1(관측 또는 Pool) |
| observation_job | 10 | 30 | Pool List4+GET4+관측 PUT/GET |
| report_read | 18 | 54 | 관측 List2+GET16 |

budget은 Store 인스턴스 수명 전체에 적용한다. Job에서 항목마다 Store를 새로 만들어 예산을 초기화하면 안 된다. SDK total_max_attempts가1~3인지 검사하고 before-send.s3 hook에서 재시도/redirect를 포함한 전송 시도마다 계측·차단한다. hook이 동작하지 않는 client는 ATTEMPT_TRACKING_UNAVAILABLE로 거부한다. 이 수치는 SDK가 전송을 시도한 횟수이지 Cloudflare가 실제 수신·처리한 요청 수를 증명하지 않는다. pre-wire 취소도 포함될 수 있다. logical_requests와 physical_attempts를 별도로 제공한다.

현재 설치 boto3/botocore **1.43.114**의 로컬 S3 서비스 모델에 IfNoneMatch가 있고, 기존 client factory를 사용한 **메모리 HTTP transport** 테스트에서 PUT 응답 유실→SDK 재시도412→GET 확인을 검증했다(logical2/physical3). 테스트는 socket 연결을 차단하고 checksum framing을 when_required로 한정했다. **실제 R2의 condition/서명/checksum/권한 호환성은 실증하지 않았다.** requirements 범위 내 모든 SDK 버전의 호환성도 검증한 것은 아니다.

### 검증 결과 및 Phase4 전 확인 사항

신규 Store 테스트67개, Phase1~3/기존 R2 Mock 회귀188개, 전체 pytest557개 통과했다. Python compile/diff check 통과, 기존 Workflow/R2 Persistence/SQLite schema/Runner/Pipeline/점수표/Telegram 코드 diff 없음. 기존 untracked/modified 파일은 보존했다. 신규 관측 Store는 Production에서 호출하지 않으며 운영 DB/API/Claim 접근, Commit/Push/dispatch를 하지 않았다.

Phase4에서는 producer가 실제 UTC request/response와 Pool Receipt SHA를 넣는지, Store context/budget을 전체 Job 동안 공유하는지, PARTIAL/NONE/UNCONFIRMED 종료 처리, SDK timeout과 Job deadline의 조합을 구현·검증해야 한다. 현재 Store는 요청 수를 제한하지만 Job 전체 deadline을 집행하지 않는다. R2 기존 factory의 connect10/read60초를 그대로 사용하므로 개별 SDK 호출이 Job 예산을 소모할 수 있다. 별도 producer 테스트/향후 test namespace의 의도된 실증 전에는 관측 Workflow를 배포하지 않는다. Phase4~7은 계속 미구현이다.


## 14. Phase4 구현 계약 — 2026-10-28

Phase1~3은 그대로 재사용하며 `services/observation_job.py`의 `run_boundary_observation(store, github, producer, clock, observation_id)`는 dependency injection 전용이다. Store와 HTTP client의 수명/close는 호출자가 소유한다. SQLite/Claim/Pipeline/Telegram/Settings 로딩은 없다. Phase3 마지막의 Phase4 미구현 안내는 그 단계 당시 기록이며 현재 상태는 이 절이 우선한다.

### 실행과 Pool 고정

1. 실제 UTC 시작 시각과 monotonic deadline을 확보한다. 기존 `report_period()`의 동일한 일요일19:00 경계를 UTC로 표현한다. 19:01은 향후 요청 예정 시각이며 Workflow를 추가/수정하지 않았다.
2. 새 `observation_job` Store 한 개에서 최근4개 완료 경계의 List4를 수행한다. 경계/LastModified/key 내림차순으로 가장 최신 manifest 하나를 GET·재검증한다. 읽기 cutoff는 Job 시작 시각이다.
3. 최신 manifest가 비어 있거나 TTL이 만료됐으면 POOL_EMPTY다. Critical overflow는 POOL_NOT_READY다. 오래된 Pool을 대신 사용해 신규 Pool에서 제거한 저장소를 부활시키지 않는다. 손상/권한/네트워크 오류도 빈 Pool로 취급하지 않는다.
4. Pool Entry의 expiry를 실제 시작 시각에 다시 확인한 뒤 ID 집합을 고정한다. TTL/이벤트 시각을 갱신하지 않는다. 이전 Snapshot은 요구하지 않는다.
5. numeric ID metadata와 weekly Trending 페이지를 조회하고 실제 request/response 시각을 기록한다. rename은 반환 metadata의 ID와 canonical 이름을 사용한다. Pool 이름만으로 Trending ID를 확정하지 않고 이번 성공 metadata 이름과 일치할 때만 확정한다. 나머지 이름은 unresolved_identity다.
6. Phase2 validator로 collection SUCCESS/PARTIAL_SUCCESS/FAILED와 coverage FULL/PARTIAL/NONE을 독립 계산·검증한다. 23시 성공 수집은 SUCCESS/NONE이며 과거19시로 보정하지 않는다. 요청이19:59에 시작해20:01에 완료되면 그 값은 경계 평가 부적격이다.
7. 같은 Store의 `advance_cutoff(completed_at)`으로 cutoff만 같은 경계 안에서 단조 증가시켜 게시한다. 모든 logical/physical budget은 그대로 유지된다. 새 `set_request_guard`는 요청 전 설치하며 logical gate와 SDK before-send 재시도 모두 검사한다. 기존 DB Persistence는 변경하지 않는다.
8. Phase3 conditional PUT/GET 검증 Receipt를 반환한다. 미확인은 Job FAILED와 PUBLICATION_UNCONFIRMED이며 수집 status와 구분한다. 게시 실패에도 이미 검증된 envelope는 result에 보존한다. 무결성 실패 envelope는 보존·게시하지 않는다.

### GitHub 어댑터와 시계

`ObservationGitHub`는 기존 GitHubClient와 collect_trending을 전용 어댑터로 감싼다. 기존 Production 호출 경로는 불변이다. repository 요청은 `/repositories/<numeric-id>` GET만 허용한다. Search/GraphQL/name 기반 요청/redirect 외부 경로는 거부한다. timeout10초를 매번 명시하고 자동 redirect는 끈다. HTTPTransport의 내부 retries가0인지 확인하며 opaque/retrying transport는 거부한다. GitHubClient의 transport/server retry도 어댑터 gate를 매번 통과한다.

metadata logical 최대40, REST 물리 시도 최대45, core reserve5를 유지한다. 인증/Rate Limit/reserve/budget에 도달하면 후속 metadata는 unavailable로 남기고 값을0으로 만들지 않는다. 이와 독립적인 Trending HTML은 한 번의 logical fetch에서 transport 실패만 backoff 후 최대2시도,403/429 즉시 재시도 없음이다. rank는 parser의 실제 페이지 순서이며 최대30/이름·중복·연속 rank를 검증한다. malformed Trending은 정상 metadata 전체를 버리지 않고 해당 source만 unavailable로 둔다.

각 logical metadata/Trending record의 시작은 첫 요청 직전, 응답 시각은 최종 응답 반환 시각이다. retry 전체 span을 보수적으로 평가한다. duration은 monotonic elapsed이며 wall-clock과의1ms 초과 불일치는 기존 validator가 fail-closed로 거부한다. 실패해 응답 자체가 없으면 observed_at=None이다. 페이지 hash는 실제 반환 HTML bytes에서 계산한다. 진단은 고정 category/HTTP/숫자형 rate headers만 담고 body/header/token 원문은 보존하지 않는다.

### 예산 및 deadline 한계

정상 R2는 List4+Pool GET1+PUT1+확인 GET1=7 logical calls다. 기존 observation_job의10 logical/30 physical 상한은 감소/초기화하지 않는다. Pool별 Store를 새로 만들지 않는다.

내부 deadline은 monotonic480초다. 150초 게시 여유를 남기고 새 GitHub 수집을 중단하며 남은 Pool 항목을 unavailable로 기록한다. 기존 R2 SDK connect10/read60을 고려해70초 이상 여유가 없으면 새 SDK logical/physical 시도를 차단한다. 이미 수집한 부분값을 정상 envelope로 검증·게시할 수 있는 경우 보존한다. R2 예산/시간 부족은 미확인이거나 안전 실패이며 성공으로 위장하지 않는다.

이는 협력적 I/O deadline이다. 실행 중인 OS socket/SDK sleep/느린 streaming body를 Python에서 강제 선점하는 process timeout은 아니다. 개별 socket timeout도 전체 wall-clock 절대 상한을 증명하지 않는다. 강제 종료 시 게시 완료를 보장하지 않으며 향후 Phase7의 Workflow timeout10분이 별도로 필요하다. 실제 R2 timeout/condition/권한 호환성은 아직 실증하지 않았다.

### 오프라인 실행 및 단계 경계

`python scripts/run_boundary_observation_once.py`는 DISABLED로 종료한다. `--mock`은 명시적 synthetic Pool/metadata/HTML과 메모리 S3/HTTP transport만 사용한다. `.env`/GitHub/R2 Secret 없이 실행되며 fixture namespace만 사용한다. `--production` 옵션은 존재하지 않는다.

동일 ID 재시도는 같은 envelope bytes를 재게시해야 한다. 같은 ID로 새로운 관측을 수집하면 bytes가 달라져 IMMUTABLE_OBJECT_CONFLICT다. 별도 Job 실행은 새 ID가 기본이며 weekly claim을 획득하지 않는다. Job 실행 성공과 VERIFIED publication을 구분한다.

Phase5에서 구현할 것은 관측 소비/선택/두 경계 비교/평가 metadata와 최신 metadata 분리 및 provenance 전달이다. Phase6 관측 consumer E2E와 Phase7 실제 관측 Workflow/Pool 발행 주체/권한·실제 timeout 검증은 미구현이다. 이번 Job은 주간 보고서에 연결하지 않았다. 기존 점수표/Evidence/Claim/R2 DB/Workflow/Telegram은 불변이다.

검증 결과: Job 신규37개, Phase1~4와 기존R2 관련237개(7.70초), 전체594개(12.82초) 통과. compileall 및 git diff --check 통과. Mock CLI는 SUCCESS/FULL/VERIFIED, REST1/Trending1/R2 logical7·physical7이었다. 테스트에서 socket 연결을 차단했으며 보호 파일(Workflow/기존R2 Persistence/SQLite/Pipeline/Runner/Scheduler/점수표/Telegram)의 diff는0이다. 이전 untracked/modified 파일을 보존했고 Commit/Push/Dispatch는 수행하지 않았다.

## 15. Phase5A Consumer 구현 계약 — 2026-10-31

이 절은5A 완료 당시의 계약/미연결 기록이다. 5B의 실제 연결과 필요한 최소 확장은16절에 기록한다.

이 절은 이전 단계의 Phase5 미구현 안내를 대체한다. 저장 관측의 읽기/검증/선택만 완료했고 **Phase5B 운영 연결은 하지 않았다**. 추가 파일은 services/observation_consumer.py, models/observation_context.py, tests/test_observation_consumer.py다. 기존 Store/Job/History/Metric/Scoring/Ranking/Runner/Pipeline/Workflow/DB/R2 Persistence는 변경하지 않았다.

### Public interface 및 불변 결과

`consume_observations(store=..., period=..., cutoff=..., repository_ids=(), legacy_snapshots=(), latest_metadata=()) -> ObservationContext`

- 전용 fresh R2ObservationStore를 profile=report_read, as_of=cutoff로 주입한다. 호출자가 client 수명/close를 소유하며 Consumer는 List/GET만 사용한다. 주간 Runner/CLI에서 자동 호출하지 않는다.
- cutoff는 명시적 UTC-aware 시각, ReportPeriod는 KST 또는 UTC의 동일한 instant로 입력할 수 있다. 최신 완료 일요일19:00 경계와 정확한168시간을 확인한다. Store의 현재/직전 경계 범위를 벗어나는 임의 과거 period는 읽지 않는다.
- repository_ids는 numeric ID 최대40개로 결과 범위를 지정한다. 빈 입력이면 bounded 읽기 집합의 ID를 반환하는 inventory 용도다. 이를 discovery/후보 자동 추가로 쓰지 않는다. detached legacy 입력 최대80개(두 경계), latest 최대40개다. 입력 tuple/list를 복사하고 객체/DB 행을 수정하지 않는다.
- ObservationContext와 모든 하위 DTO는 frozen dataclass/tuple이다. Repository별 current/previous SnapshotSelection, latest, SnapshotComparison, trending_rank와 전역 TrendingSelection, diagnostics, boundary read coverage 및 선택 결과 summary를 제공한다. for_repository(id)는 해당 결과 또는 None이다.
- provenance에는 source_type/source_url/object_key/object_id/검증된 payload SHA/request_started_at/observed_at/시각 정밀도를 보존한다. metadata도 validator의 tuple/frozen 모델을 사용한다. 임의 body/Token/오류 원문을 결과에 넣지 않는다.

### 읽기·cutoff·실패

현재와 이전 boundary를 각각 한 번 List하고 그 고정 reference 집합만 GET한다. 정상 최대2 LIST+16 GET=18 logical/54 SDK physical 예산이며 Store cap을 그대로 재사용한다. 소비 도중 cutoff를 갱신하지 않으며 Store cutoff가 외부에서 변경되면 CONSUMER_CUTOFF_CHANGED로 거부한다.

List의 LastModified<=cutoff, GET의 같은 reference/LastModified 및 key/payload/namespace/schema/출처/actual time/canonical SHA를 검사한다. 이후 게시된 객체를 재List해 같은 실행에 끼워 넣지 않는다. 보고서가23시에 시작해도 실제19:01 관측은 적격이며,23시 새 관측은 시간 검증에서 제외한다.

정상 빈 목록은 EMPTY/OBSERVATION_MISSING이다. List에 있던 객체가 GET에서 없어지면 LISTED_OBJECT_MISSING으로 구분한다. 인증/권한은 기존 AUTHENTICATION_OR_PERMISSION과 HTTP401/403으로 구분 가능하며 network/잘린 page/상한/JSON/hash/namespace/source 오류는 기존 Store/Validator category를 보존한다.

**한 객체라도 읽기·검증 실패하면 해당 실행의 R2 관측 집합 전체를 배제**하고 OBSERVATION_STORE_UNAVAILABLE로 남긴다. 읽지 못한 객체가 더 이르거나 충돌할 수 있으므로 이미 읽힌 일부 결과를 정상적인 완전 집합처럼 사용하지 않는다. Legacy로 접근 실패를 감추지도 않는다. latest 입력은 별도 검증하여 보존하지만 비교에 쓰지 않는다. 운영 DB restore/수정/네트워크 fallback은 없다.

boundary coverage의 COMPLETE/EMPTY/UNAVAILABLE은 읽기 완전성이다. COMPLETE가 비교 가능/근거 충족을 의미하지 않는다. summary는 current/previous 확인 수, Star 비교 가능 수, 충돌/미사용/누락 Repository 수, Trending 상태를 별도로 집계한다.

### Snapshot 선택 및 비교

Phase2의 개별 적격성(요청 시작과 실제 응답 모두 경계 후0~3600초, 응답<=완료<=cutoff)을 통과한 같은 ID/Boundary 값만 사용한다. 가장 이른 응답, 동률이면 요청 시작→object ID→key 사전순으로 고정한다. 새로운 관측 시각으로 기존 값을 보정하지 않는다.

같은 ID/응답 시각의 Star/Fork 또는 metadata가 서로 다르면 OBSERVATION_CONFLICT다. Legacy와 객체 간의 동시각 값 차이도 source 우선순위로 숨기지 않는다. 가장 이른 값 뒤에 나타난 동시각 충돌도 전체 검사한다. 이 버전은 보수적으로 그 boundary Snapshot 전체를 선택하지 않고 Star/Fork 비교를 unknown으로 둔다. 충돌 source 참조를 보존하고 평균/최고/최신/대체 관측을 고르지 않는다.

증가량은 current boundary-previous boundary만 계산한다. 성장률은 기존 metric_service.growth를 재사용한다. 이전0이면 not_applicable, 누락/충돌/미사용이면 unknown, 감소/실제0은 confirmed다. 예: previous1000/current1200/latest1500 => delta200/growth20%/latest1500. 양쪽 observed_at 및 실제 간격(초)을 보존하므로 관측 간격이 무조건168시간이라고 주장하지 않는다.

### Legacy 호환의 안전 경계

`legacy_from_weekly_snapshot(snapshot, request_started_at=None, fixture=False)`는 기존 models.domain.WeeklySnapshot을 detached LegacySnapshot으로 바꾸는 순수 어댑터다. id:<numeric>만 허용하고 이름/URL에서 ID를 추정하지 않는다. 기존 measured_at/period_end는 동등한 UTC instant로 표현할 뿐 원본을 수정하지 않는다.

현재 운영 WeeklySnapshot에는 요청 시작 시각이 없다는 한계가 확인됐다. 이를 measured_at으로 만들어 넣으면19:59~20:01 요청을 잘못 승인할 수 있다. 따라서 **시작 시각이 별도 실제 기록으로 주입되지 않은 행은 LEGACY_REQUEST_TIME_MISSING으로 제외**한다. fixture도 같은 계약을 따른다. 별도 기록이 있는 Legacy는 양 시각/window/cutoff/ID/count/fixture를 검사하고 같은 선택기에 넣는다. 기존 history_service의 단일 measured_at 정책은 변경하지 않았다.

Legacy provenance는 sqlite:weekly_snapshots/batch_completion을 명시한다. object_key와 R2 payload SHA는 None이며, legacy-<row-content digest>를 결정적 동률 식별자로 쓴다. 이를 R2 객체 SHA 또는 개별 API 응답 정밀도라고 표시하지 않는다. 부적격 row는 category를 남기고 삭제/갱신하지 않는다. Consumer는 DB handle이나 SQL을 받지 않는다.

### Trending 선택 및 근거 보호

현재 boundary의 같은 weekly scope에서 유효한 가장 이른 페이지 전체 하나를 선택한다. 페이지별 순위/이름/검증 ID/HTML digest를 보존한다. 같은 응답 시각에 페이지 hash 또는 entries/ID mapping이 다르면 TRENDING_CONFLICT로 전체 페이지를 배제한다. 같은 페이지 중복은 새 신호를 만들지 않는다.

페이지 hash 형식과 그것을 포함하는 envelope payload SHA를 재검증한다. 원본 HTML 자체는 저장 계약에 없으므로 Consumer가 HTML hash를 원문에서 다시 계산한다고 주장하지 않는다. 실제 HTML digest 계산은 Phase4 Job의 책임이다.

같은 envelope의 metadata와 매칭되어 확정된 ID entry만 rank를 제공한다. 미해결 이름을 다른 객체나 최신 metadata로 승격시키지 않는다. 페이지에 없거나 페이지가 누락/충돌이면 rank는 unknown이며 False로 만들지 않는다. 여러 페이지의 최고 rank를 결합하지 않는다. 이 순위는 실제 observed_at의 rolling page 순위이며 정확히19:00 상태라고 표현하지 않는다.

Context에는 signals/evidence_sources를 생성하는 기능이 없다. 단순 저장/중복 조회/Pool 입장은 독립 근거가 아니며 star/trending/activity/discussion/release/security/policy 분류와 score_candidate/derive_signals/validation/ranking은 불변이다.

### Phase5B 연결 계획 — 아직 미구현

| 대상 파일 | 후속 변경 계약 |
|---|---|
| services/weekly_live_runner.py | 기존 R2 DB restore/Claim 사전 검사 후 consumer factory 주입 준비. 중복 Claim이면 관측 List/GET도 없음. 새로운 DB fallback 없음 |
| services/pipeline.py | 기존 원자적 live Claim 획득 후 consumer를 실제 한 번 실행. discovery로 찾은 ID만 context에 연결하며 Pool/관측 전체를 후보로 삽입하지 않음 |
| services/history_service.py / services/metric_service.py | typed boundary comparison과 provenance를 명시적으로 전달. late stars와 previous boundary를 섞거나 observed_at만 보정하는 경로 금지. 기존 activity/이벤트 정책 유지 |
| Pipeline 평가 Candidate adapter | score_candidate의 T와 표시 Star는 같은 boundary 관측값 사용. latest_metadata는 별도 보존. metadata가 없는 Legacy만으로 T/age/relevance를 혼합하는 처리는 활성화 전 별도 검증 |
| services/persistence_service.py | 기존 Snapshot에는 report의 실제 latest metadata와 실제 측정시각을 전달. boundary1200을23시 latest1500의 측정시각으로 저장하지 않음. 관측 정본은 append-only 객체 유지, SQLite import/migration 없음 |
| telegram/formatter.py 및 출력 직렬화 | 평가값/observed_at와 최신값을 별도 라벨로 표시하고 provenance/unknown/conflict를 유지 |
| 관련 통합·회귀 테스트 | Claim 우선/두 값 분리/별도 시간/서버 장애/fixture isolation/점수표·Evidence 불변을 검증한 뒤에만 활성화 |

Phase5A에는 위 파일을 수정하지 않았다. Phase6 Consumer E2E와 Phase7 관측 Workflow/실제 배포도 미진행이다.

Phase5A 검증 결과: 신규 Consumer52개(0.63초), Phase1~5A/기존R2/Claim/History 관련322개(7.75초), 전체646개(9.70초) 통과. Python compileall/git diff --check 통과. 소비 테스트는 socket 연결을 차단하고 FakeS3/Synthetic Observation/가상 cutoff/Legacy DTO만 사용했다. 운영 DB/실제 R2 객체에는 접근하지 않았고 기존 Workflow/Pipeline/Runner/SQLite/R2 DB Persistence/Claim/History/Metric/점수표/Evidence/Ranking/Telegram의 diff는0이다. 기존 working tree 보존, Commit/Push/Dispatch 없음.

## 16. Phase5B Production 연결 계약 — 2026-10-31

이 절의 Phase6 미구현 안내는5B 완료 당시 기록이다. 이후 Phase6 Mock E2E 결과는17절에 기록한다.

### 실제 연결 순서와 Claim 보호

기존 run_weekly_live_once의 설정/운영 R2 restore/Claim 사전 검사/finalize/integrity/upload 및 종료 코드는 유지한다. 기본 production_pipeline factory가 ReportPipeline에 lazy production_observation_loader(settings)를 주입한다. loader 생성 시 SDK client/R2 List/GET은 발생하지 않는다.

Pipeline은 기존 atomic claim_live_run을 가장 먼저 성공시킨 뒤 discovery와 attach_history를 수행한다. 그 직후 고정 UTC cutoff를 잡고, 읽어 둔 Legacy row를 Consumer에 전달하여 한 번 조회한다. 이후 numeric ID로 발견 후보에만 결과를 적용한 뒤 기존 metrics/validation/score/select_top/deep analysis/narrative/formatter/splitter/send/persist_week를 실행한다. 이 순서는 Legacy 및 최신 metadata 확보가 필요해서 권장 흐름의 Consumer 위치를 discovery/history 뒤로 둔 최소 조정이다.

사전 Claim 또는 atomic Claim이 막히면 Consumer/관측 List/GET이 없다. Claim 실패/기존 SUCCESS/FAILED/RUNNING/PARTIAL_SUCCESS의 종료·보존 정책은 불변이다. 관측 실패는 별도 DB restore/Claim 변경/강제 재실행을 유발하지 않는다. 직접 생성한 Pipeline은 loader가 없으면 기존 경로를 유지한다. loader가 주입돼도 dry-run/preview/integration-test에서는 호출하지 않으며 fixture 주입과 live mode 혼합은 기존처럼 금지한다.

### 평가 데이터와 최신 데이터

Candidate에 latest_repository/latest_observed_at/observation을 추가했다. 기존 repository는 관측 연결 시 평가용 copy이며, 최신 metadata를 deepcopy하여 별도로 보존한다. JSON 직렬화는 이전 artifact도 읽고 새 latest Repository도 모델로 복원한다.

| 용도 | 사용하는 값 |
|---|---|
| Star T/derive_signals/score_candidate | 같은 ID의 검증된 current boundary Star/Fork 및 비교 Metric |
| age | boundary metadata가 있으면 그 created_at, 없으면 기존 GitHub created_at(관측시각 대체 금지) |
| relevance/category/API 접근 이름·URL | 기존 discovery 판정 및 현재 canonical 경로 유지; 관측으로 관련성/우선순위 승격 없음 |
| 신규 탐색 priority | 기존 의미대로 latest Star/created_at 기반 discovery-only 지표 유지 |
| Issue/PR/Release/Security/Discussion | 기존 조회/예산/독립 근거 판정 유지 |
| SQLite Repository/Snapshot/Watchlist/history Repository | 실제 discovery 최신 Repository와 실제 배치 완료시각 |
| Telegram | 현재 Star/최신 Fork와 평가 기준 Star/Fork·각 관측시각을 구분 |

예: previous1000/current1200/latest1500은 delta200/growth20%, T의 규모는1200이다. 기존 score 함수는 수정하지 않는다. metrics.calculate_metrics는 관측 연결 시 이미 검증된 Fork delta/growth를 Legacy/최신 혼합으로 다시 덮지 않는다. 그 외 계산은 기존대로다. Unknown 경계값을 latest로 채우지 않는다. 현재 경계가 없으면 평가 Star/Fork=None이지만 실제 latest1500은 저장·표시하며, 활동/공식 사건 등 나머지 신호로 계속 평가한다.

최신 metadata 시각은 기존 collector가 제공하는 discovery **배치 실제 완료시각**이다. 개별 API 응답 정밀도라고 주장하지 않고, Consumer의 개별 request/response와 별도로 남긴다. 이전 DB row의 measured_at을 보정/삭제하지 않는다. 새 관측 원본은 append-only R2 객체에 유지하며 SQLite로 import하지 않는다.

### Consumer/Legacy 우선순위와 실패

기존5A의 기본 earliest 정책은 유지한다. 이번 요청의 Observation 우선→유효 Legacy fallback을 위해 Consumer에 선택적 prefer_object_snapshots=False 인자를 추가했다. Production loader만 True를 전달한다. 검증/모든 동시각 충돌 검사를 먼저 끝내고, 유효 객체가 있으면 그 집합의 earliest를 선택하며 없으면 유효 Legacy 중 earliest다. 동시각 Legacy/객체 충돌을 우선순위로 숨기지 않는다. 이를 제외한 validator/List/GET/hash/cutoff/페이지/불완전 집합 fail-closed 계약은 바꾸지 않았다.

history_service는 기존 report history/이전 score/watchlist/원본 Snapshot을 읽는다. 읽은 Legacy DTO만 Consumer에 전달하고, 별도 실제 request_started_at이 없다면 LEGACY_REQUEST_TIME_MISSING으로 제외한다. Legacy 행에 현재 시각이나 measured_at을 요청 시작으로 주입하지 않는다. Consumer는 운영 DB를 열지 않는다.

반환 context의 production namespace/기간/cutoff/ID 중복/하위 snapshot ID·경계를 확인한다. 다른 이름이 같아도 ID가 다르면 매칭하지 않는다. fixture/wrong-period/wrong-ID context는 optional source unavailable로 거부한다. R2 접근·손상·SHA·source 오류는5A의 안전한 진단과 전체 집합 배제를 유지한다. loader 예외는 고정 category/HTTP만 기록하며 임의 예외 문자열을 출력하지 않는다. 운영 DB restore/Claim/integrity 같은 기존 치명적 오류는 여전히 기존 종료 정책을 따른다.

### Evidence/출력/Persistence

관측 파일/Pool/context를 signals에 추가하지 않는다. star delta/growth는 기존 star 하나이며 rank가 유효할 때 기존 trending 하나만 적용한다. 기존 MIN_SIGNALS=2/Critical/score/ranking/repeat 규칙과 discovery/shortlist 순서·예산은 변경하지 않았다. 관측 Inventory 중 discovery에서 발견되지 않은 ID는 후보에 추가하지 않는다.

structure_evidence에는 평가 Star/Fork의 basis/observed_at 및 latest 값·시각 metadata만 추가한다. Fact/opinion/actor/quote/impact validation과 prompts는 불변이다. 실제 OpenAI 출력 품질·token 변화는 이번 Mock만으로 실증하지 않았다. 저장된 rolling Trending이 유효한 경우 reliability의 live-page 부재와 저장 관측 사용을 구분하며, 정확히19:00 상태라고 표시하지 않는다. 기존 funnel의 historical_trending_unavailable은 live discovery 미사용 의미를 유지하고 저장 Trending 확인 수는 새 집계에서 구분한다.

Persistence는 latest_repository를 사용해 repositories/weekly_snapshots를 저장하고 최신 metadata를 report history/watchlist에도 유지한다. weekly_metrics에는 평가 Metric/score와 safe observation provenance를 기존 JSON 안에 보존한다. history에는 새 observation metadata를 넣되 기존 row를 갱신하지 않는다. schema/7개 테이블은 그대로다. 성공/실패 recovery 모두 실제 최신값을 저장하며 경계1200을23시 측정값으로 저장하지 않는다.

Formatter 레이아웃은 유지하고 관측 연결 항목에 최신 Fork/최신 metadata 관측시각/평가 기준 Star·Fork/Trending 실제 관측시각만 덧붙인다. Sender/Splitter/retry 코드는 불변이며 메시지 길이 처리는 기존 splitter가 담당한다. artifact replay에서도 두 값 분리가 유지된다.

### 별도 Candidate diagnostics

기존 candidate funnel/exclusion/data-gap 집계는 변경하지 않는다. 다음9개를 candidate 단위로 별도 집계한다:

- observation_context_loaded: numeric ID가 context에서 매칭된 후보 수
- observation_context_missing: 매칭된 context가 없는 후보 수(접근 실패는 unavailable에도 집계)
- current_boundary_confirmed / previous_boundary_confirmed
- star_comparison_confirmed / trending_confirmed
- observation_conflicted / observation_unavailable
- legacy_snapshot_used: current 또는 previous에 검증된 Legacy가 선택된 후보 수

성공/부분 성공 result와 실패 automation_runs.report에 같은 observation_diagnostics를 보관한다. InsufficientCandidates에는 exception metadata로 전달하고 Runner는 allowlist로 검증한 같은9개 값을 콘솔에 출력한다. 개별 provenance와 safe category는 candidate artifact 및 score/history JSON에 남긴다. 원본 응답/Secret은 남기지 않는다.

### 미구현 및 운영 게이트

이번 단계는 code wiring과 Mock component/regression 검증이다. 관측 Job→R2 publication→Consumer→report의 별도 Phase6 전체 E2E는 아직 구현/완료 처리하지 않는다. Phase7 관측 Workflow/Pool 발행 운영 주체/권한/실제 SDK·R2 호환성/실행 시간·token·Telegram 운영 실증·배포는 미진행이다. 기존 정규 Workflow에는 손대지 않았다. 새 코드가 실제 배포되면 기본 production factory는 관측 R2 읽기를 시도하므로 배포 전 이 후속 게이트와 관측 객체 확보 상태를 확인해야 한다.

이번 작업은 실제 GitHub/R2/OpenAI/Telegram에 접속하지 않고 임시 SQLite/FakeS3/MockTransport만 사용했다. 실제 운영 DB/과거 FAILED Claim/보고 이력 변경, 과거 재실행, Workflow 수정·Dispatch, Commit/Push는 수행하지 않는다.

Phase5B 검증 결과: 신규 연결43개, Consumer52개 유지, 연결·기존 Pipeline/History/Scoring/Validation/Ranking/Scheduler/R2/Weekly Runner 관련402개(13.29초), 최종 전체689개(15.39초) 통과. Python compileall/git diff --check 통과. 기존 Workflow/SQLite schema/DB Claim/R2 DB Persistence/score/validation/ranking/critical/duplicate/Scheduler/Sender/Splitter 파일 diff=0이다. Evidence 서비스에는 basis/latest metadata만 추가하고 validation 규칙은 바꾸지 않았다. 기존5A의 미연결 AST 테스트는 이제 정책 함수가 Consumer를 직접 import하지 않는지 검사하도록 명칭/범위를 갱신했고, Claim 이후 lazy 호출 및 non-live 미호출은 새 연결 테스트에서 검증한다. 실제 API/운영 DB/배포/과거 Claim 재실행/Commit/Push/Dispatch는 수행하지 않았다.

## 17. Phase6 Observation 전체 Mock E2E — 2026-10-31

### 실제 실행 경로

추가 파일은 tests/observation_e2e_support.py와 tests/test_observation_e2e.py다. 기존 DemoBackend/ObservationS3/FakeS3/VirtualClock/live_settings를 재사용하되 수작업 Observation JSON이나 예상 Context를 Pipeline에 넣지 않는다.

`build_pool → seal_pool → R2ObservationStore.publish_pool → run_boundary_observation(ObservationGitHub/GitHubClient/collect_trending) → conditional PUT/verification GET → 저장 bytes → consume_observations → Claim된 ReportPipeline → Evidence/Scoring/TOP5/Deep Analysis/Narrative/Formatter/Splitter/TelegramSender → persist_week → checkpoint/integrity/export → R2DatabaseStore.upload`

두 경계의 Job이 각각 metadata REST와 실제 Trending parser를 실행한다. HTTP 응답만 MockTransport로 제공하고 Job이 만든 모델을 validator/canonical JSON/SHA 계약에 따라 Store가 게시한다. Consumer는 같은 메모리 버킷의 SDK GET Body를 읽는다. 성공 테스트는 게시 후 Job 결과 참조를 지워도 Consumer가 값·시각·object key/SHA를 복구하는지 검증한다. Producer 반환값을 Consumer 입력으로 쓰지 않는다.

운영 주간 구간은 실제 run_weekly_live_once를 통해 실행한다. 기존 offline 설정 검증→Fake R2 운영 DB restore→Claim 사전/원자적 검사→discovery/history→Consumer→평가/선정→TOP5만 분석/Mock 전송→SQLite 확정/검증/연결 종료→R2 백업/업로드를 사용한다. 두 R2 client는 같은 메모리 objects dictionary를 공유하여 namespace 분리 검사가 단순히 별도 버킷을 사용한 결과가 아니도록 한다.

### Synthetic namespace와 최소 주입 확장

모든 관측/Pool payload는 fixture=True이고 test-observations/v1/ 및 test-observation-pools/v1/에만 게시한다. 실제 Production namespace로 바꾸거나 fixture=False로 위장하지 않는다. 관측 client는 production/·backups/뿐 아니라 production 관측 prefix도 거부한다. Fake DB client만 메모리의 production/github_weekly_report.db와 backups/를 취급한다. 이는 운영 객체가 아니라 pytest 임시 DB의 bytes다.

기존 Production Context 검증기가 fixture를 거부하므로 전체 테스트 합성에는 최소 DI가 필요했다. ReportPipeline에 observation_validator 선택적 인자(기본 기존 validate_pipeline_context), 해당 함수에 namespace 선택적 인자(기본 production)를 추가했다. 테스트 factory만 namespace=fixture를 명시하고 기존 기간/cutoff/ID 검증을 그대로 실행한다. codec/Producer/Store/Consumer 검증은 모두 실제 fixture 계약으로 실행한다. 실제 Production factory/CLI에는 이 override를 추가하지 않았다. fixture Context를 기본 Production 검증기에 넘기면 계속 FIXTURE_NAMESPACE_MISMATCH이며 해당 지표는 unknown이다. 테스트 namespace 객체를 Production Store에 게시하려는 시도도 SDK 호출 전에 거부함을 확인한다.

Candidate.observation JSON에 context namespace를 보존하여 fixture 출처가 artifact/score/history에서 사라지지 않게 했다. SQLite Claim mode=live는 임시 Fake 운영 경로의 원자적 Claim/전송 정책을 검증하기 위한 것이며 실제 운영 DB에 쓰지 않는다. 이 테스트 전용 합성을 실제 배포 옵션으로 노출하지 않는다.

### 검증된 값과 데이터 흐름

| 값 | Previous | Current boundary | Latest discovery | 검증 |
|---|---:|---:|---:|---|
| A Stars |1000|1200|1500|delta200/growth20%, 평가 T1200, 저장·표시1500|
| A Forks |100|150|200|delta50/growth50%, 저장·표시200|
| B Stars |2000|2300|1500|delta300/growth15%, T2300; latest1500의 T와 점수가 다름을 검증|

가상 시각은 명시적 Synthetic Fixture다. 이전/현재 경계 후19:01부터 metadata 응답마다0.1초를 전진시켜 실제 request/response/duration을 모델에 기록하고 보고서 Runner는23:00에 시작한다. 수집/게시/소비가 Clock을 과거로 소급 보정하지 않는다. 별도23시 지연 Job은 SUCCESS/NONE이며 Window 밖 metadata를 평가에 쓰지 않는다.

Current/Previous missing, latest-only, 감소, previous0 not_applicable, Trending unavailable, 동일 시각 두 Producer의 값 충돌, 동일 객체 GET2회, JSON/hash 손상, 접근 오류, 잘린 목록을 전체 경로에서 검증한다. 느린 PUT acknowledgement가8분 협력 deadline을 넘긴 경우 게시 확인은 UNCONFIRMED이고, payload의 실제19:01 시각은 유지하되23시 cutoff 이후 LastModified 객체는 소비에서 제외한다.

실제 derive_signals/validate_candidate/score_candidate/select_top을 실행한다. 동일 Star의 delta/growth 및 중복 게시만으로 독립 근거가 늘지 않는다. 별도 Mock Release/주간 Issue·PR/Security가 충분하면5개 서로 다른 프로젝트를 선정한다. Star 하나만 있고 활동/공식 사건/Trending이 없거나, 독립 신호가 부족하면 InsufficientCandidates다. Critical 후보는 기존 mandatory inclusion으로 포함한다. Evidence 구조화·actor/quote/impact 검증·Narrative도 실제 코드이며 OpenAI 응답만 Fake이다.

Mock Telegram에는 최신값/평가기준값/실제 관측시각/summary/TOP1~5/alert/watchlist가 생성된다. 기존 paragraph를 늘려 splitter가 length/UTF16 상한을 지키고 같은 TOP part를 연속 배치함을 검증한다. 일부 실패는 최대3attempts 후 다음 항목으로 진행하며 PARTIAL_SUCCESS, 전부 실패는 FAILED다. Schema7개/최신 Snapshot/metrics score/소개 이력/Watchlist/각 전송 로그를 저장하고 integrity/foreign key 검사 및 WAL checkpoint/export 후 독립 DELETE-journal DB를 업로드한다.

### 장애 및 복구 행렬

| 주입 위치 | 결과/보존 정책 |
|---|---|
| Pool 생성 | 미래 입력이 실제 Builder에서 FUTURE_DATA로 거부됨; 새 객체 없음; 다른 정상 자료로 주간 처리 가능 |
| metadata/Trending | 실제 Job의 unavailable/부분 성공/coverage를 보존; 나머지 Source로 계속 평가 |
| Observation PUT drop/timeout | 실제 Store가 UNCONFIRMED 또는 commit 후 같은 key GET VERIFIED로 구분; 불확실한 게시를 성공 처리하지 않음 |
| Consumer JSON/SHA/403/network/pagination | 구체 category/OBSERVATION_STORE_UNAVAILABLE 및 unknown 유지; 다른 사건으로 평가 |
| discovery/Evidence validation/Report 생성의 예외 | local FAILED Claim, 실제 최신값 수집분 복구; 원격 DB 업로드 안 함(기존 unexpected-failure 정책) |
| 근거 부족 | InsufficientCandidates/FAILED Claim/최신 Snapshot을 정상 finalize 후 원격 DB에 보존; 가짜 보고서/전송 없음 |
| Telegram 일부/전체 실패 | 기존 retries/log/부분 또는 FAILED 상태 보존; 원격 Claim 기록 후 재실행 차단 |
| DB restore transport/손상 | RESTORE_FAILED, 주간 클라이언트/관측 조회 없음; 빈 DB fallback 없음 |
| checkpoint/final integrity | SQLITE_FINALIZE_FAILED, 업로드 안 함; 원격 기존 DB 유지 |
| DB backup/upload | R2_UPLOAD_FAILED, 기존 원격 DB 유지; upload 실패 시 기존 백업도 보존; remote claim 미확정/중복 발송 위험 표시 |

예상 밖 예외에서 local FAILED가 기록됐다고 remote FAILED까지 보존됐다고 주장하지 않는다. Telegram 성공 뒤 업로드 실패도 완전 성공이 아니다. 자동 재전송/Claim 삭제/원격 롤백/원격 객체 삭제는 없다. Local physical SQLite는 주입한 오류 이후에도 실제 integrity 검사를 통과하는지 확인한다.

처리된 SUCCESS/FAILED/RUNNING/PARTIAL_SUCCESS를 담은 Fake 원격 DB 및 같은 주차 replay는 관측 List/GET·GitHub/OpenAI/Telegram 없이 SKIPPED_ALREADY_CLAIMED다. 재실행은 새 임시 local path로 원격 Claim을 다시 복원하는 새 호스트 상황을 재현하며, 기존 실패 Claim bytes를 수정하지 않는다. Claim 획득 자체가 실패할 때도 관측/API work가 없다.

### 보호·결과·한계

테스트 autouse guard가 socket.connect/create_connection, 실제 httpx.HTTPTransport 생성, R2 SDK factory, Settings.load를 pytest 실패로 차단한다. 실제 .env/Secret은 읽지 않는다. API wrapper 클래스는 실제 Production 코드지만 HTTP와 S3 구현은 Fake/MockTransport다. 모든 파일은 pytest 임시 디렉터리이며 운영 DB/기존 R2 객체/과거 FAILED Claim에 접근하지 않는다. Workflow/Dispatch/Commit/Push 없음.

기능 버그는 이번 E2E에서 확인되지 않았다. E2E 합성을 위한 validator 주입과 namespace audit metadata만 운영 소스에 최소 추가했다. 동일 객체 두 번 GET하는 신규 테스트에서 호출0회로 잘못 기대한 assertion은 실제 GET2회를 확인하도록 수정한 테스트 오류였으며 Production 동작 변경으로 해결하지 않았다.

신규 E2E43개(8.33초), Phase1~6/기존 통합·R2·Runner 관련382개(21.29초), 전체732개(24.35초) 통과. Python compileall/git diff --check 통과. 기존 Workflow/SQLite schema/Claim/R2 DB Persistence/History/Metric/Persistence/Scoring/Evidence/Ranking/Scheduler/Formatter/Sender/Splitter 파일 diff=0이다.

Phase7은 미완료다. 실제 R2 IfNoneMatch/서명/checksum/권한/네트워크 timeout, GitHub/OpenAI/Telegram 실 API, 실제 token usage/서술 품질은 이번 Mock이 증명하지 않는다. Pool 발행 운영 경로/관측 Workflow/스케줄 지연·실행 시간·권한 최소화·초기 실제 관측 확보와 배포 게이트를 별도 작업에서 검증해야 한다. 기본 Production validator는 그대로 production namespace만 기대하며 이번 테스트용 validator override를 운영 설정으로 복사하지 않는다.

## 18. Phase7A Observation 전용 validate Workflow — 2026-11-02

관측 전용 .github/workflows/observation.yml과 scripts/validate_observation_deployment.py, tests/test_observation_workflow.py를 추가했다. 기존 weekly-report.yml/One-shot Runner/R2 DB Store/SQLite backup·schema/Claim/Report History/Scoring/Evidence/Telegram은 수정하지 않았다. 이 절은 이전 단계의 Phase7 전체 미구현 기록을 세분화하며, 현재 완료는7A뿐이다.

### Workflow 실행 계약

- 이름 Observation System, event는 workflow_dispatch만 사용한다. mode는 choice/default/options 모두 validate 한 가지다. job도 dispatch+validate 조건을 확인한다. schedule/workflow_run/자동 Weekly 연계/Production job·command는 없다.
- 기존과 같은 Ubuntu/actions checkout@v6/setup-python@v6/Python3.11/pip requirements 설치 및 pip check를 사용한다. checkout persist-credentials=false, permissions contents:read만 허용한다. 내장 checkout token은 이 최소 권한으로만 사용하며 서비스 토큰을 환경변수에 넣지 않는다.
- concurrency group은 github-weekly-report-observation으로 기존 github-weekly-report-production과 분리한다. cancel-in-progress=false, queue=max다. [GitHub 공식 concurrency 문서](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)에서 이 조합을 확인했다. queue=max는 최대100개 대기이며 큐가 가득 차면 신규 실행이 취소될 수 있으므로 무제한 대기를 보장하지 않는다.
- timeout20분은7A의 dependency/오프라인 테스트 job 한도다. 향후 실제 관측 Job의8분 내부 deadline/10분 Workflow 제안과 혼동하지 않는다.
- 단계는 checkout/Python/dependencies → validate_observation_deployment → Observation 관련 pytest → 전체 pytest → Python compile → git diff --check → always Summary다. 검증/테스트 실패는 비정상 exit code 그대로 job 실패가 된다. continue-on-error/실패 무시/Production bootstrap 옵션은 없다.
- Summary에는 Workflow/event/Python target/각 step outcome/Production NOT EXECUTED만 표시한다. R2 endpoint, DB 경로, Secret, 파일 내용은 넣지 않는다. upload-artifact는 없으며 setup-python의 pip dependency cache만 사용한다.

### Offline validator 범위

검증기는 Settings.load/os.environ/.env를 사용하지 않는다. Settings dataclass의 필수 필드/기본 timezone, 관측 모듈 imports, frozen model 주요 필드, Pool/Validator/Store/Job/Consumer/Pipeline의 callable signature, schema/객체 크기/Pool/API/deadline 상한과 예산 profile 존재를 검사한다. 필요한 dependency imports 및 소스의 메모리 compile을 확인한다. API/R2 client를 생성하거나 운영 SQLite를 열지 않는다.

socket/SQLite connect/HTTP client/SDK client와 Settings.load에 일시적 금지 guard를 설치하고 검사 후 복구한다. 실패 로그는 고정 category/프로젝트 상대 모듈·소스 이름만 출력하며 예외 원문/Secret 일부·길이·접두사도 출력하지 않는다. validator exit1 또는 잘못된 CLI 옵션 exit2는 성공으로 바꾸지 않는다. --production 옵션은 없다.

필수 설정 검사는 **코드 구조/default 계약**만 의미한다. 현재 단계에서 Secret 전달을 금지하므로 실제 환경의 token/bucket/endpoint 값 존재·형식·권한은 검사하지 않는다. 기존 Production validate_live/validate_r2 정책을 완화하거나 대체하지 않았으며 실제 설정/연결 검사는7B 이후 별도 승인 단계다. Import/Mock 성공을 실제 R2 인증/conditional PUT 실증으로 보고하지 않는다.

### 사용자 검증 절차

1. 로컬에서 python scripts/validate_observation_deployment.py 및 python -m pytest tests/test_observation_workflow.py -q를 실행한다.
2. 사용자가 diff를 확인해 필요한 변경을 기본 브랜치에 반영한다(이번 작업은 Commit/Push하지 않음).
3. Actions → Observation System → Run workflow에서 최신 반영 branch/mode=validate를 선택한다. 이번 작업에서 Dispatch하지 않았다.
4. validator/Observation/full tests/compile/diff가 success인지, Summary에 Production NOT EXECUTED 및 connectivity NOT TESTED가 표시되는지 확인한다. schedule이 없고 기존 Weekly Workflow의 권한/예약/Secret mapping이 그대로인지 확인한다.
5. 이 validate 성공만으로 관측 수집/Production 옵션/예약을 활성화하지 않는다.

### Phase7B~7E 제안과 Secret 격리 — 전부 미구현

| 단계 | 후속 구현/검증 제안 |
|---|---|
| 7B | Observation 전용 R2 bucket/자격증명/Reader·Writer 역할 및 실제 SDK/권한/conditional PUT 실증. 실제 호출은 별도 승인 작업 |
| 7C | Pool 발행 운영 주체와 실제 수집 가능한 one-shot 인터페이스/수동 전용 Workflow 준비 |
| 7D | 제한된 실 관측→소비 운영 검증, 지연·누락·복구·예산 확인. 기존 Claim/보고 발송은 별도 명시적 범위 |
| 7E | 운영 검증 후 관측 예약/monitoring/retention/알림·배포 활성화 |

7B Secret 이름 제안은 OBSERVATION_R2_ENDPOINT/OBSERVATION_R2_BUCKET와 OBSERVATION_R2_READER_ACCESS_KEY_ID/OBSERVATION_R2_READER_SECRET_ACCESS_KEY, OBSERVATION_R2_WRITER_ACCESS_KEY_ID/OBSERVATION_R2_WRITER_SECRET_ACCESS_KEY다. 아직 코드·Workflow·Settings·GitHub Secrets에 추가하거나 연결하지 않았다. 기존 R2_ENDPOINT/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET 및 GH_API_TOKEN/OpenAI/Telegram Secret 이름·권한을 바꾸지 않는다.

Reader는 관측/Pool List·GET, Publisher는 해당 prefix의 List·GET·conditional PUT이 필요한 역할로 설계한다. production/·backups/의 DB 접근 권한을 관측 writer에 주지 않는 구조가 목표다. 공급자 IAM의 prefix별 제한 지원을 이번 단계에서 검증했다고 주장하지 않는다. **별도 Observation bucket을 우선 검토**하며 실제 bucket 범위 권한과 현재 loader 설정 분리는7B에서 확인·구현해야 한다. 기존 Consumer loader는 현재 R2 설정을 사용하므로 제안 Secret만 등록해도 자동 연결되는 것은 아니다. Delete/retention은 별도 관리 역할로 검토하며 validate에는 어떤 자격증명도 주입하지 않는다.

### 검증 결과와 미검증 항목

신규 Workflow/validator 테스트18개, 기존 Weekly Workflow와 합쳐64개(1.68초), Observation 관련355개(24.59초), 최종 전체750개(32.17초) 통과. validator 직접 실행 PASS (offline only), Python compileall/git diff --check 통과. 기존 Weekly Workflow는 LF/CRLF 정규화 SHA-256 fixture로 불변을 검사한다. 기존 Production 파일 diff=0이며 운영 DB/API/Claim/과거 run/Commit/Push/Dispatch 접근 없음.

확인한 외부 자료는 GitHub 공식 Workflow concurrency 문서뿐이며 서비스 데이터 API는 호출하지 않았다. GitHub Actions 서버의 실제 Workflow 실행/권한 및 R2 실 호환성은 미검증이다. YAML 파싱/정적 검사 성공을 서버 배포 완료로 보고하지 않는다. Phase7B~7E는 미구현·미배포 상태를 유지한다.

## 19. Phase 7B — 격리 테스트 bucket R2 probe 구현

18절의7B 미구현 및 제안 Secret 이름은 당시 기록이다. 이번 사용자 요구에 따라 **OBS_R2_ENDPOINT/OBS_R2_ACCESS_KEY_ID/OBS_R2_SECRET_ACCESS_KEY/OBS_R2_BUCKET 네 개**를 쓰는 전용 probe를 구현했다. 이전 OBSERVATION_R2_* 제안은 바인딩하지 않는다. 기존 Production Settings/R2 factory/Consumer loader/DB Store/두 기존 Workflow는 변경하지 않는다. Phase7B **코드·Mock 준비 완료, 실제 연결 NOT TESTED**이며 bucket/자격증명/Environment 생성과 사용자 live 실증이 남는다.

`services/observation_r2_probe.py`는 frozen 비밀필드 repr 제외 설정·안전 category/HTTP status·불변 단계 결과를 제공한다. Settings.load/.env/기존R2_* fallback 없이 OBS 설정만 검사한다. HTTPS Cloudflare 계정/jurisdiction endpoint, 전용 `gwr-observation-test-` bucket을 client 생성 전에 검증한다. 기존 typed Observation Store의 namespace를 바꾸지 않고 별도 `phase7b-probe/<run>/<attempt>/<nonce>/probe.json`만 사용한다. 계정 전체·운영 DB·백업 객체 접근 API가 없다.

설치된 SDK PutObject 모델의 IfNoneMatch 지원을 확인하고 signed scoped List→conditional PUT→GET(bytes/size/SHA/metadata)→두 번째 conditional PUT412→원본 GET→소유권 GET→정확한 key 삭제 순서다. SHA256은 bytes로 계산하며 ETag를 대체 hash로 쓰지 않는다. 정상7회/첫PUT lost-ack 복구8회, 논리 호출 상한10회, SDK total_max_attempts=1, connect10/read15초다. GET stream 최대64KiB/close 및 client finally close를 적용한다. SDK schema/공식 문서 지원을 실제 R2 실증으로 주장하지 않는다.

첫 PUT timeout/network/5xx는 같은 key를 양성 GET으로 확인해야 성공이다. 미확인은 UNCONFIRMED, 새 key 재시도 및 소유권 불명 삭제 금지다. 두 번째 PUT도 timeout이면 진행 중 요청 앞에서 원본을 삭제하지 않고 Lifecycle로 정리한다. 기존 객체의 초기412는 실패·삭제 금지다. 조건부 쓰기 미지원/일반PUT 덮어쓰기는 실패다. cleanup 소유권 검사는 atomic delete 계약이 아니므로 전용 bucket·임의 UUID·직렬화가 전제이며 불일치 시 남겨 둔다. lifecycle은 관리자가 테스트 bucket prefix에만 설정한다.

새 `observation-r2-check.yml`만 추가했다. dispatch-only validate(Secret/Environment 없음)와 live(Environment observation-r2-test+명시적 confirm)로 나눈다. contents:read, 별도 concurrency github-weekly-report-observation-r2-check/cancel=false/queue=max, timeout20/10분, Ubuntu/Python3.11/Actionsv6를 사용한다. live step에 OBS Secret 네 개만 주입하고 기존R2/OpenAI/Telegram/GH 서비스 Secret은 전달하지 않는다. GitHub Environment 승인 규칙은 사용자가 설정해야 하며 Secret scope 원본은 런타임에서 증명할 수 없다. Repository/Organization에 같은 OBS 이름을 등록하지 않도록 안내한다.

validate는 기존7A offline guard 검사와 SDK schema 검사를 재사용하고 Mock tests를 실행한다. live만 실제 client를 만들 수 있다. CLI 기본validate/비동의live 차단, provider message/헤더/SDK요청/본문/설정 값 없이 고정 category+HTTP만 출력한다. 안전 Summary 외 artifact upload는 없다. exit0=검증·정리 성공, exit1=실패/미확인, exit2=검증 성공 후 정리 경고. 실제 permission403/서명 mismatch 원인은 단정하지 않는다.

설정·Cloudflare 공식 근거·환경 격리 한계·사용자 실행·token 폐기는 [OBSERVATION_R2_CHECK.md](OBSERVATION_R2_CHECK.md)에 정리했다. Phase7C Pool 발행·수동 실 관측 실행경로,7D 운영 실증,7E 정규 관측 예약은 계속 미구현이다. 기존 Scoring/Evidence/Schema/Claim/주차 경계/보고 전송 경로는 불변이다.
