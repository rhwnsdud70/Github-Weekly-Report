# 예약 지연과 후보 관측 안정성 점검 — 2026-10-27

## 실행 사실과 이번 변경 범위

사용자 제공 2026-10-25 schedule 실행은 예정 19:00 대비 Created/Started가 모두 23:02:45 KST로 4시간 2분 45초 늦었다. 생성 이후 대기보다 이벤트 생성 자체가 늦었다는 점은 관측되지만 GitHub 내부 원인은 확인할 수 없다. 이번에는 실제 GitHub/OpenAI/Telegram/R2 API나 운영 DB에 접근하지 않고 코드/Mock만 사용했다. 해당 주차의 FAILED claim을 변경하거나 재실행하지 않는다.

Workflow 요청 시각만 `7 19 * * 0`, `Asia/Seoul`로 변경했다. 이는 정각 요청을 피하는 완화이며 정시 실행 보장이 아니다. `report_period()`는 그대로라 19:07/19:30/23:00에도 `10/18 19:00 <= event < 10/25 19:00`의 168시간이다. 마스터 사양의 19:00 보고 경계는 유지하며 요청 시각 7분 변경은 이번 사용자 지시에 따른 명시적 운영 예외다.

수동 validate/production, schedule→production, concurrency/queue/Secrets/R2 객체/Claim/중복 발송 방지/점수표/독립 근거 최소 2개/Critical 예외는 변경하지 않았다. 새 관측 Workflow나 schema 변경은 구현하지 않았다.

## Snapshot 누락의 의미

1. Pipeline은 이번 discovery에서 반환한 후보 전부의 Repository metadata를 보존한다. 정량 Issue/PR/Release 조회는 처음 10개 및 Critical에 제한되지만 Star/Fork Snapshot 저장은 그 목록에만 제한되지 않는다.
2. 정상 완료에는 `persist_week()`가, 실패에는 기존 복구 경로의 `persist_measurements()`가 Snapshot/Metrics를 저장한다. 실패는 성공 보고서 소개 이력/Watchlist/Telegram 성공 상태를 만들지 않는다. 처리된 InsufficientCandidates에서 terminal FAILED/SQLite 검증 후 기존 R2 upload가 관측값과 claim을 함께 보존한다. 예상 밖 예외의 R2 업로드 정책은 별개이며 변경하지 않았다.
3. 후보로 재발견되지 않은 저장소는 별도로 갱신하지 않는다. 저장된 Watchlist는 다음 discovery에서 재조회하지만 성공적으로 선별·저장된 Watchlist가 없으면 이 경로도 없다. 기존 주요 후보 전체를 다음 주 자동 재관측하는 기능은 없다. 따라서 매주 새 후보가 많거나 발견 집합이 바뀌면 이전 Snapshot 확보율이 낮다.
4. `previous_snapshot(identity, period.start)`는 ID와 정확한 이전 period_end 문자열로 조회한다. `previous_snapshot_missing:31`은 이 조회에 해당하는 행이 없다는 뜻이다. 신규/미재발견/이전 수집 실패/다른 ID·주차·표현 등의 개별 원인은 운영 DB를 읽지 않아 확정할 수 없다. 일반 production은 동일한 KST 문자열을 저장·조회하므로 포맷 불일치를 이번 원인으로 단정하지 않는다.
5. 행이 있더라도 현재 관측 시각이 current end 후 0~3600초이면서 이전 measured_at도 previous end 후 0~3600초여야 비교한다. 4시간 지연이면 이전 행이 정확했더라도 현재 조건 때문에 unusable이다. 따라서 이번 2개의 이전 행도 늦게 관측됐다고 단정할 수 없다. 새로운 `current_observation_outside_window`와 `previous_observation_outside_window` 집계가 이를 구분한다.
6. R2 restore는 기존 행을 복원하고 Pipeline이 같은 조회를 수행한다. 다음 주에도 같은 ID/직전 주차/시각 조건을 만족하면 비교된다. R2 저장 자체가 늦은 관측이나 없는 과거 값을 복구하지는 않는다. Cold Start는 19:07 변경만으로 해결되지 않는다.

확인된 최소 버그 수정:

- Snapshot 행이 있어도 비교 시각이 부적합하면 출처가 늘 `previous snapshot missing`으로 기록되던 진단 오류를 수정했다. absent/window 초과/현재 Star 미확보/이전 Star 미확보를 구분한다. 값은 계속 unknown이다.
- 수집 전 시각을 모든 Snapshot에 사용하면 19:59에 시작한 metadata 수집이 20:01에 끝나도 허용 시간 내 관측으로 판정할 수 있었다. 신규 저장은 실제 metadata 배치 **완료 시각**을 사용하여 이 경우 보수적으로 비교를 차단한다. 이전 저장 행은 변경하지 않는다. 이는 개별 API 응답 시각을 기록하는 완전한 관측 모델이 아니라 배치 시각이며, 향후 개별 관측 구조에서 정밀화한다.

## Trending과 지연 진단

live Trending은 저장된 과거 API가 아니라 현재 `https://github.com/trending?since=weekly` HTML이다. 기존 3600초 상한을 유지했다. 19:07/19:30은 시간상 eligible이며 실제 fetch 성공/관련성/추가 근거를 보장하지 않는다. 23:00은 ineligible, rank/score는 unknown이며 별도 API 실패로 집계하지 않는다.

Runner는 실제 시작 KST, 19:00 보고 경계, restore 이후 preflight 시각, 종료 후 경과 초/시분초, Trending/Snapshot의 시간상 허용 여부, 시각 때문에 사용할 수 없는 데이터를 출력한다. Snapshot 비교에는 이전 행의 조건도 필요함을 명시한다. Pipeline은 실제 수집 시각에 독립적으로 재검사하며, preflight가 허용됐다고 관측 성공으로 간주하지 않는다. 이 경과 시간은 경계부터 애플리케이션 시점까지이며 GitHub 이벤트 생성 지연/queue/R2 소요 시간을 분해하거나 GitHub 내부 원인을 추정하지 않는다.

실제 현재 Star/Fork 값 자체를 버리지는 않는다. unknown이 되는 것은 경계 Snapshot을 사용한 7일 비교이다. 기간 내 Release/Issue/PR/Security는 정확한 반개방 기간으로 수집·평가를 계속한다.

## Independent Evidence 점검

독립 신호는 `star`, `trending`, `activity`, `discussion`, `release`, `security`, `policy`의 set이다. source/API/문서 개수를 합산하지 않는다.

| 데이터 | 인정 방식/한계 |
|---|---|
| Repository Search | discovery와 metadata/relevance 확보용. 검색에 노출됐다는 사실만으로 독립 근거 추가 안 함 |
| Issue/PR | 정확한 기간 created 집계 및 전주 활동 증가. Issue/PR 두 API여도 activity 하나 |
| Release | period 내 release_event → candidate.events → derive_signals → validate로 전달됨. feature 이상에 해당하는 의미 있는 Release만 신호, patch/단순 minor는 신호를 보장하지 않음 |
| Issue 사건 | 공식 author association 및 명시적 title 패턴의 정책/API/종료 사건만 policy 신호. 일반 Issue가 존재한다는 이유로 별도 신호 추가 안 함 |
| Security | period 내 advisory 사건이 security 신호. Critical은 기존 강한 단일 근거 예외 유지 |
| Discussion | 수집된 성장 근거가 있으면 평가 가능하지만 production candidate 단계는 discussion_total을 deep 단계까지 지연함. 이번에는 추가 API 요청을 만들지 않음 |
| 중복 사건 | ID 병합 및 신호 set, score max 처리로 같은 사건/API 반복이 근거 수나 점수를 곱하지 않음 |

Mock으로 Star/Trending 없이 실제 형식의 주간 activity+Major Release를 가진 서로 다른 후보 5개가 기존 `select_top()`을 통과하는 것을 검증했다. 같은 Issue/PR 활동 또는 같은 release의 반복으로 독립 신호가 증가하지 않는 것도 검증했다. 확인한 경로에서는 수집된 유효 이벤트가 검증기에 전달되지 않는 버그를 재현하지 못했다. 다만 production 자료 전체를 재수집하지 않았으므로 해당 주차 Release 분류/소스별 실패 여부까지 확정할 수 없다.

기존 수집 한계도 남는다. 정량 조회는 발견 순서의 처음 10개 및 Critical이며, 나머지 후보에는 충분한 event가 이미 없다면 신호를 확보하기 어렵다. Discussion을 최종 선정 뒤 읽는 구조도 candidate 단계의 근거 확보를 제한한다. 이는 이번에 validation 기준을 바꾸어 해결하지 않으며, 후속 단계에서 budget 내 조회 대상 선정/관측 pool을 별도 검토한다.

## 후속 관측 방안 비교 — 설계만 제안

아래 비용은 현재 대비 상대적 요청/저장량으로 비교하며 실제 요금 추정은 하지 않는다.

| 기준 | A. 경계 근처 경량 관측 | B. 주간 중 정기 경량 관측 | C. Trending 없이 현재 사건 평가 |
|---|---|---|---|
| 복잡도 | 중간: 별도 job/저장/선택 규칙 필요 | 높음: 시계열/중복/보관 정책 필요 | 낮음: 현재 경로가 지원 |
| API 요청량 | 주 1회 pool N개 metadata REST + Trending HTML 1회 수준 추가 | 예: 6시간마다면 주28회 × pool metadata 및 HTML 관측 | 추가 Trending/관측 요청 없음; 현재 candidate budget 유지 |
| Actions 시간 | 짧은 관측 job 추가 | 짧은 job을 여러 번 추가 | 추가 job 없음 |
| R2 저장/전송 | 주1회 관측 객체; DB 전체 재전송보다 append-only 객체 검토 | 작은 객체가 더 많이 쌓임; retention 필요 | 현행 DB 전송만 |
| Cold Start | 관측 pool은 다음 경계부터 개선; 신규 repo 과거 값은 계속 unknown | 사전 발견 확률 상승; 경계 관측 없으면 정확한 7일 Star는 여전히 unknown | 해결 못 함; 실제 사건 조합으로만 평가 |
| 지연 대응 | 이후 분석 job이 늦어도 저장 관측 재사용 가능. 관측 job도 같은 Actions라면 지연 위험 유지 | 특정 관측 누락에 강함. 여러 관측을 과거 경계값으로 치환하면 안 됨 | 지연돼도 timestamp가 있는 사건은 조회 가능; Trending unknown 유지 |
| 신뢰성 | 실제 observed_at/출처/period 검증 필수 | 실제 관측만 사용. 주중 rank를 주말 rank라고 쓰지 않는 규칙 필요 | 기존 사실만 평가; 충분한 독립 근거 없으면 계속 실패 가능 |
| schema 호환 | 경계 Star/Fork 1회는 기존 weekly_snapshots 재사용 검토 가능. Trending 관측의 영구 보존·claim 분리 설계 필요 | 기존 identity+period_end 단일행은 다회 관측에 부족; 새 table 또는 별도 R2 객체 필요 | schema 변경 없음 |

권장 순서(이번에는 구현하지 않음):

1. 현재 C 경로와 새 진단으로 source coverage를 확인한다. 근거 gate와 1시간 상한은 유지한다.
2. 다음 단계 승인 후 A를 우선 설계한다. 직전 주요 후보/Watchlist/소개 프로젝트를 ID로 관리하는 제한된 관측 pool을 정하고 boundary metadata/Trending을 수집한다. 새로운 repo의 이전 수치는 만들지 않는다.
3. 관측은 관측 전용 run/object로 저장하며 weekly report claim을 획득하거나 발송하지 않는다. 기존 R2 운영 DB에 두 writer가 경합하지 않도록 production concurrency를 공유하거나 `observations/` 같은 별도 append-only 객체를 사용한다. schema/migration 및 소비 시점은 구현 전에 확정한다. 스케줄러 분리는 Actions 자체 지연을 제거하지 않으므로 필요하면 더 신뢰할 수 있는 실행원을 별도 검토한다.
4. observed_at, repo ID, source URL/type, 수집 성공 상태, 실제 값, 무결성 정보를 보존한다. 경계에 맞지 않는 Star 관측은 기존 허용 범위 밖이면 사용하지 않는다. 저장 Trending의 period/rank 적용 규칙도 먼저 확정하여 현재 점수표를 우회하지 않는다.
5. 필요성이 입증될 때 B로 확장한다. 보관 기간/요청 상한/R2 객체 수/원자성/중복 관측/실패 복구/운영 DB와 통합 규칙을 추가 테스트한다.

## 다음 예약에서 확인할 것

이 변경을 사용자가 기본 브랜치에 반영하고 validate를 통과한 뒤 schedule cron이 19:07인지 확인한다. 다음 run의 보고 종료는 여전히 일요일 19:00이어야 한다. Actual runner start/Observation preflight time/Collection delay 및 eligible 상태를 함께 본다. 실패 시 기존 funnel과 새 current/previous window 초과 사유를 확인하고 FAILED claim/SQLite integrity/R2 upload를 확인한다. 이미 저장된 10/25 FAILED 주차는 재실행하지 않는다. Codex에서 배포/dispatch하지 않는다.
