# Phase 7B~7C — 실제 데이터를 사용하는 격리 Observation 실행

## 현재 확인 범위

사용자는 2026-11-02 Environment `observation-r2-test`, bucket `gwr-observation-test`에서 연결 검사 Configuration/인증200/Bucket200/PUT200/GET200/SHA256/Conditional PUT412/Cleanup204 성공을 보고했다. 이는 **기존 probe 객체 작업 실증**이며 새로운 Pool→Job→Store→Consumer 실증과 다르다.

이번 코드는 기존 builder/seal_pool/validators/R2ObservationStore/run_boundary_observation/consume_observations를 연결했다. 새 경로는 Mock 검증만 수행했으며 **실제 Pool·Observation 게시 및 Consumer 실연동: NOT TESTED**다. 현재 Codex 프로세스에는 네 OBS 설정이 없으며 GitHub Environment Secret 조회/Actions Dispatch/Push를 하지 않았다. 실제 경계 시각을 가상으로 만들어 서비스에 게시하지 않았다.

## 환경·namespace 격리

- 네 자격증명 설정은 `OBS_R2_ENDPOINT`, `OBS_R2_ACCESS_KEY_ID`, `OBS_R2_SECRET_ACCESS_KEY`, `OBS_R2_BUCKET`만 읽는다. Settings.load/.env/기존R2_* fallback이 없다.
- 별도 settings adapter는 실제 bucket **`gwr-observation-test` 또는 `gwr-observation-test-<suffix>`만** 허용한다. 기존 probe의 suffix 필수 정책/Workflow는 변경하지 않았다. 이번 명시적 bucket 승인에 맞춘 새 runner 전용 allowlist다.
- client는 사용자가 명시적 live mode/확인을 선택했을 때만 생성한다. `verify`는 HTTP client도 생성하지 않는다. validate는 설정값·Secret·client·SQLite를 읽지 않는다.
- GitHub는 자동 `github.token`의 contents:read만 전달한다. GH_API_TOKEN/기존 Production R2/OpenAI/Telegram Secret은 전달하지 않는다. 전용 R2 token은 테스트 bucket 하나로 제한해야 하며 기존 운영 token을 재사용하지 않는다.
- 모든 Store는 `namespace='fixture'`로 고정한다. 실제 key는 `test-observation-pools/v1/YYYY-MM-DD/<uuid>.json`, `test-observations/v1/YYYY-MM-DD/<uuid>.json`이다. 날짜는 기존 KST 경계 날짜이며 schema/version/canonical SHA-256을 바꾸지 않았다.
- **기존 schema에서 fixture=True는 테스트 namespace에 필요한 라우팅 표시**다. 여기서는 실제 GitHub 응답 값을 합성·추정하지 않는다. console/Summary는 `data_origin=live-github; namespace=fixture (test routing)`로 구분한다. Mock 테스트의 가상 Clock/HTTP는 Synthetic Fixture로만 실행한다. Production validator/Context gate는 이 namespace를 계속 거부한다.
- SDK wrapper는 bucket 일치와 두 테스트 prefix의 JSON/date/UUID 형태를 다시 검사한다. 임의 key 인자, production/·backups/·운영 SQLite 접근 경로가 없다. 기존 Weekly Consumer 설정과 연결하지 않는다.

## 모드와 실행 순서

새 Workflow: **Observation Test Runtime**, `.github/workflows/observation-test-runtime.yml`.

| 모드 | 실행 경로 | 네트워크/전제 |
|---|---|---|
| validate (기본) | 기존7A 구조 검사+SDK schema+새 adapter 메모리 compile+Mock tests | Secret/Environment/client 없음 |
| pool | 최근4주 Pool의 주차별 최신 manifest 조회→실제 공개 metadata discovery→기존 builder→seal_pool→조건부 PUT/GET 검증 | 전용 테스트 R2+공개 GitHub. 첫 Pool 없이도 실제 discovery로 시작 가능 |
| boundary | 기존 Job→최신 유효 Pool 고정→numeric ID metadata→weekly Trending→검증/게시 | 유효 Pool 및 실제 경계 후0~3600초 필요 |
| verify | 실제 R2 current/previous List/GET→Consumer→불변 Context/coverage/diagnostics | R2만. GitHub/운영 SQLite/Legacy DB 조회 없음 |
| full | pool→boundary→verify | 각 단계에서 실제 현재 시각 재확인, 동일 테스트 bucket의 저장 bytes 재조회 |

workflow_dispatch만 사용하고 schedule/push/PR/기존 Workflow 자동 연계는 없다. 별도 concurrency `github-weekly-report-observation-test-runtime`, cancel=false/queue=max, contents:read, Ubuntu/Python3.11/기존Actionsv6/requirements, timeout20분이다. validate와 isolated-live job을 분리하고 live만 `observation-r2-test` Environment를 사용한다. 확인값 false는 credential 주입 전에 exit1이다. 사용자가 Environment 승인자/branch 보호를 설정한다. Environment의 네 OBS Secret을 모두 등록하고 같은 이름의 Repository/Organization Secret을 두지 않아 scope fallback을 방지한다.

사용자 입력은 env→인용한 Bash 배열→argparse로 전달한다. shell source에 repository 문자열을 삽입하지 않는다. 서버의 기본 GITHUB_REPOSITORY/RUN_ID/RUN_ATTEMPT/SHA를 Producer에 보존한다. secret/endpoint/bucket 값/headers/긴 본문은 Summary에 출력하지 않는다. 실행 ID, key, SHA, 실제 request/observed 시각, safe HTTP/status/집계만 남기며 artifact upload는 없다.

## Pool과 요청 예산

DB 없는 첫 실행은 이번 사용자 요구에 따라 **실제 공개 discovery를 명시적으로 허용**한다. 이전 설계의 운영 DB read-only bootstrap 제안은 이 테스트 runner에서 사용하지 않는다. Production 데이터 bootstrap 정책은 바꾸지 않는다.

`repositories`에 공개 owner/repo를 쉼표로 최대8개 지정할 수 있다. 이름을 안 주면 기존 collect_trending으로 현재 weekly 페이지의 처음8개를 discovery하고 실제 metadata로 relevance/exclusion을 검증한다. Total Star 정렬/과거 Star 추정/가짜 Watchlist·TOP5·사건은 없다. 페이지를 Pool discovery에 사용했다고 해당 주의 boundary Trending 근거로 주입하지 않는다. Boundary Job이 별도로 실제 시각에 관측한 페이지만 rank로 검증한다.

최근4개 완료 주차에서 주차별 LastModified→key 내림차순의 최신 manifest 하나씩, 최대4GET을 읽고 기존 builder로 numeric ID 병합한다. rename과 first_seen/TTL/우선순위는 기존 계약 그대로다. 네트워크·손상·권한·불완전 목록이면 새 빈 Pool로 덮어쓰지 않는다. 신규8/전체40/7·14·28일 규칙 및 Critical overflow를 유지한다. metadata 재조회만으로 의미 있는 사건 시각을 갱신하지 않는다. 새 discovery와 이미 만료된 동일 ID가 병합되면 기존 first_seen/expiry 때문에 재입장이 거절될 수 있다. 이는 TTL 보존 결과이며 강제로 갱신하지 않는다.

| 단계 | 상한 |
|---|---|
| Pool discovery | metadata 최대8 logical, REST 최대12 attempts, Trending HTML 최대1회(이름 미지정 시), Search/GraphQL/LLM0 |
| Boundary Job | 기존 metadata40/REST45/Trending HTML logical1·attempt2, HTTP10초, deadline8분 |
| R2 Pool read | 기존 report_read profile18 logical/54 physical 내 List4+GET최대4 |
| R2 Pool publication | 기존 publication2 logical/6 physical |
| R2 Boundary | 기존 observation_job10 logical/30 physical; Pool List/GET와 게시 예산 공유 |
| R2 Consumer | 기존 report_read18 logical/54 physical, current/previous 각 최대8객체 |
| 선택 cleanup | 이번 실행의 acknowledged Pool/Observation 최대2개, 각 GET1+Delete1 |

전용 client factory의 SDK retry는 total_max_attempts1이다. Store의 before-send tracking과 profile 상한도 유지한다. HTTP pool retry는 기존 GitHubClient 방식 안에서12 physical gate로 제한하고 core reserve5를 보존한다. full의 Pool 단계 remaining을 Boundary adapter에 전달해 단계 전환으로 reserve를 초기화하지 않는다. numeric metadata도 private:false를 요구한다. 공개 metadata 외 Issues/Search/GraphQL/LLM은 추가 호출하지 않는다.

## 시간·실패·충돌

report_period()의 일요일19:00 KST/168시간/반개방 기간을 재사용한다. boundary/full은 실제 현재 시각이 경계 후0~3600초가 아니면 **client 생성 전 거절**한다. pool 생성 뒤에도 시간을 재검사한다. 요청이 허용 창 안에서 시작했더라도 응답이 창을 넘으면 기존 Job은 실제 시각/collection status와 coverage NONE을 그대로 보존한다. Consumer는 이를 평가에서 제외하며 runner는 완전 성공이라고 표시하지 않는다. 주중 full을 강제 허용하거나 과거 시각/경계 override 옵션을 제공하지 않는다.

이전 경계가 없으면 비교 unknown이 정상이다. current와 previous를 만들기 위해 이번 주 값을 과거 key에 게시하지 않는다. 첫 실제 경계에서는 current만 검증하고, 다음 실제 주차에 양쪽 경계를 비교해야 한다. verify의 cutoff는 호출 시작에 고정되고 이후 LastModified 객체는 제외한다.

Store가 기존 조건부 PUT/GET/schema/SHA/exact bytes 검증을 담당한다. PUT timeout은 같은 ID/key만 GET으로 확인하고 UNCONFIRMED를 성공으로 표시하지 않는다. 동일 key/동일 bytes는 멱등 성공, 다른 bytes는 IMMUTABLE_OBJECT_CONFLICT다. 같은 시각의 다른 객체 값 충돌은 Consumer CONFLICT다. 잘린 목록/손상/permission/network를 정상 EMPTY로 취급하지 않는다. runtime 자체의 무한 retry/새 key로 자동 실패 재시도는 없다. 사용자가 다음 invocation을 실행하면 새 UUID/실제 시각을 가진 별도 관측이다.

기존 최대8객체/주차 List 상한을 유지하므로 같은 주차에 수동 모드를 무제한 반복하지 않는다. 넘으면 fail-closed이며 production 안전장치를 늘리지 않는다.

exit0는 해당 모드의 완전 성공, exit2는 partial/missing/cleanup warning, exit1은 실패다. 첫 full의 previous missing/Trending missing 등은 Summary에 명시되고 exit2가 될 수 있다. 이를 7일 비교 완료로 해석하지 않는다. Pool 없음/새 후보 없음은 실패이며 가짜 repository로 채우지 않는다. verify 정상 EMPTY와 storage UNAVAILABLE을 별도로 표시한다. 일부 게시 뒤 후속 단계가 실패해도 기존 immutable 객체는 기본 보존되어 나중에 재검증 가능하다. Claim/운영 보고 상태에는 영향이 없다.

## 보존과 cleanup

기본은 테스트 객체 보존이다. 이전 경계와 Pool을 다음 주 재사용해야 하기 때문이다. 사용자가 `cleanup_current_run=true`를 명시하면 전체 모드 후 이번 실행에서 HTTP200으로 PUT acknowledgement를 받은 정확한 key만 원본 bytes GET 확인 후 삭제한다. 초기412/PUT lost-ack는 삭제 소유권으로 인정하지 않으며 기존 객체·다른 실행 key·prefix·bucket은 삭제하지 않는다. cleanup 오류는 경고와 비정상 exit를 남긴다. GET→DELETE는 원자적 conditional delete가 아니므로 테스트 bucket/UUID/직렬화와 다른 writer 금지가 전제다.

테스트 bucket 관리자에게 test-observations/v1/와 test-observation-pools/v1/ 보관기간을 최소35일로 설정하는 것을 권장한다(최근4주·지연 검증용). probe의 phase7b-probe/ 1일 만료와 구분한다. runtime은 Lifecycle/bucket 설정 권한을 쓰지 않는다. 강제 종료/미확인 PUT의 잔존 key는 사용자 승인 정리/Lifecycle로 처리한다. 기본 실행에서 remote delete는 전혀 하지 않는다.

## 사용자 수동 실행 절차

1. 사용자가 diff를 검토해 반영한다. 이번 작업에서 Commit/Push/Dispatch하지 않았다.
2. 이미 준비한 `observation-r2-test` Environment의 OBS 네 Secret과 테스트 bucket 제한 권한/승인자를 확인한다. 기존 Weekly/DB token은 전달하지 않는다.
3. Actions → **Observation Test Runtime** → `validate` 실행. Secret 없이 검사·Mock tests가 성공하는지 확인한다.
4. `pool`, `confirm_isolated_bucket=true`를 선택한다. 필요한 경우 `repositories`에 `damianvtran/local-operator` 등 실제 공개 AI/developer repository를 지정한다. Pool count/key/SHA/VERIFIED와 safe HTTP 상태를 확인한다. 이름이 없어도 임의 인기 목록을 대신 생성하지 않는다.
5. **실제 일요일19:00~20:00 KST 안에서** `boundary`를 실행한다. Pool이 없으면 먼저 pool 또는 full을 선택한다. Actions 요청 자체가 늦어1시간을 넘으면 실패가 올바른 결과다. schedule은 추가하지 않았다.
6. `verify`로 실제 R2 GET→Consumer 검증을 확인한다. 첫 주 previous missing은 정상적인 unknown이다. 다음 주 실제 경계 후 verify에서 두 boundary 비교를 확인한다. 삭제 옵션은 비교 완료 전 기본false로 둔다.
7. full을 원하면 동일 시간 창에서 `full`+확인을 선택한다. 최초 실행의7일 비교/정규 Production 성공을 주장하지 않는다. 정리 권한을 별도로 검사하려면 cleanup_current_run=true를 명시하고 해당 실행 key만 삭제됐는지 확인한다.

로컬 구조 검사는 `python scripts/run_observation_test_once.py`, `python -m pytest tests/test_observation_test_runtime.py -q`다. 로컬 live는 안전하게 주입된 OBS 환경변수와 Producer용 GITHUB_REPOSITORY/RUN_ID/RUN_ATTEMPT/SHA가 모두 있어야 한다. 운영 .env를 load하지 않는다. 실제 값은 shell history/명령 인자에 적지 않는다.

```powershell
python scripts/run_observation_test_once.py --mode pool --confirm-isolated-bucket --repositories damianvtran/local-operator
python scripts/run_observation_test_once.py --mode boundary --confirm-isolated-bucket
python scripts/run_observation_test_once.py --mode verify --confirm-isolated-bucket
```

## 남은 실증과 Phase7D

이번 작업의 offline E2E는 Synthetic GitHub 응답/가상 Clock/FakeS3를 쓴 실제 Producer→Store bytes→Consumer 경로다. 사용자 제공 connectivity 결과를 새 runtime의 실제 성공으로 승격하지 않는다. 실제 Pool/Boundary/Consumer/cleanup/SDK before-send tracking과 서버 Workflow 승인은 사용자 실행 후에만 확인 가능하다.

Phase7D는 이 테스트 namespace에서 실제 두 주차 관측·지연/누락·예산·충돌·회복을 점검하고, 별도 운영 Observation bucket/Reader·Writer credential/Pool 운영 공급원을 확정하는 단계다. 이번 테스트 token/namespace를 Production Weekly loader에 넣는 작업은 포함하지 않는다. 기존 DB/Claim/Scoring/Evidence/19:07 Workflow를 바꾸거나 정규 관측 schedule을 활성화하지 않는다.

오프라인 최종 결과: 신규47/Observation 관련455/전체850 passed. Python compile 및 git diff --check PASS. 기존 세 Workflow/Production 파일 불변. 실제 Pool/Boundary/Consumer/R2 정리는 NOT TESTED.
