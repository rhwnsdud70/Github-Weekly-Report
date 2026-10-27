# Production InsufficientCandidates 점검 (2026-10-16)

후속 변경(2026-10-27): Workflow 요청 시각은 19:07로 변경하고 보고 경계 19:00 및 3600초 제한은 유지했다. 실제 실행 지연 진단/현재·이전 관측 초과 사유와 신규 Snapshot 배치 완료 시각 기록을 추가했다. 상세 원인 및 관측 설계는 [DELAY_AND_OBSERVATION_PLAN.md](DELAY_AND_OBSERVATION_PLAN.md) 참조. 아래는 10월 16일 점검 기록이다.

이번 점검은 코드와 로컬 자료 읽기, 임시 DB/Mock 테스트만 사용했다. R2 다운로드/업로드, 실제 API 호출, 운영 DB 변경, 기존 claim 변경 및 schedule 활성화는 하지 않았다.

## 확인된 사실과 확인 한계

사용자 제공 Actions 결과의 대상은 `2026-10-04T19:00:00+09:00 <= event < 2026-10-11T19:00:00+09:00`이다. R2 복원, FAILED terminal claim 확정, SQLite 검증, R2 업로드까지 성공했으며 이는 배포/저장 실패와 구분되는 애플리케이션 실패이다. `InsufficientCandidates`는 원칙적으로 검증·재소개 제한을 통과한 저장소가 5개 미만일 때 발생한다. 별도로 Critical 저장소가 5개를 초과하여 필수 포함과 TOP5가 충돌할 때도 같은 예외를 사용한다. 기존 콘솔은 예외 이름만 보여 어느 분기인지 확정할 수 없다.

10월 16일 실행에는 10월 11일 종료 후 1시간이 지났으므로 코드상 live Trending 수집이 제외된다. 이는 확인된 수집 조건이며 API 장애가 아니다. 이것만으로 이번 주차의 모든 제외 사유를 확정할 수는 없다. 실제 funnel/후보별 신호/이전 Snapshot 유무 및 API budget 소진 여부는 해당 실행 자료가 있어야 확인할 수 있다.

로컬 `data/github_weekly_report.db`는 읽기 전용 immutable 연결로 집계만 확인했다(WAL 없음). 그 파일에는 10월 11일 종료 run이 없고, 로컬 candidate artifact의 period도 9월 6일~13일이다. 해당 artifact의 evidence-supported=0을 이번 Actions 결과로 간주하지 않는다. 로컬 DB는 현재 R2 객체의 사본이라고 확인되지 않았으며 R2 최신 객체를 읽지 않았다. 기존 R2 FAILED claim은 그대로 보존한다. 새 진단은 향후 실행부터 저장되며 과거 기록을 소급 수정하지 않는다.

## 실제 후보 흐름

| 항목 | 코드상 의미 |
|---|---|
| discovered | metadata 조회에 성공하여 discovery의 add에 들어간 횟수; 여러 source의 중복 포함 |
| deduplicated | collector의 limit 적용 후 Pipeline에 전달된 Repository ID 기준 후보 수; limit에서 잘린 전체 고유 후보 수와 다름 |
| metrics attempted | 처음 10개 및 모든 Critical 후보; 후보 40개 전부의 심층/정량 조회가 아님 |
| metrics completed | attempted 후보 중 issues/pulls/releases_checked 모두 unknown/unavailable이 아닌 후보; 모든 지표의 완전성을 뜻하지 않음 |
| relevance eligible | AI/developer 관련성이 있고 exclusion rule에 걸리지 않은 후보(Critical 예외 유지) |
| evidence supported | 독립 signal 2개 이상 또는 Critical; 문서 수나 collection source 개수가 아님 |
| scoring eligible | 계산된 score가 있으며 relevance/evidence/validation 사유로 제외되지 않은 후보; 별도 재소개 제한과 구분 |
| selectable | 최종 ranking 검증 후 validation_reasons가 없는 후보 |

이 수치는 일부는 병렬 집계이므로 항상 단조 감소하지 않는다. 제외 사유와 data gaps도 서로 겹칠 수 있다. `derive_signals()`는 star, trending, activity, discussion, release, security, policy의 독립 신호를 만든다. Issue와 PR 활동은 합쳐 activity 하나이며, 검색 결과에 등장했다는 사실만으로 독립 신호를 추가하지 않는다. 단순 patch release도 release 신호를 만들지 않는다. `MIN_SIGNALS=2`, Critical 예외, 관련성, 중복/재소개 제한은 변경하지 않았다. 현재 ranking에는 grade를 이용한 별도 최소 총점 탈락 조건이 없으며, 점수/grade는 유효 후보의 순위에 사용한다. 낮은 grade만 보고 이번 실패를 점수 threshold 문제라고 단정하면 안 된다.

## 실행 시각과 Cold Start

- `report_period()`는 가장 최근 완료된 일요일 19:00 KST를 end로 하고 168시간 전을 start로 한다. 일요일 18:59:59는 직전 주 종료를 선택한다. 몇 분/며칠 지연돼도 해당 주 end를 실제 실행 시각으로 옮기지 않는다.
- live Trending은 Pipeline에서 확인한 시각과 period.end 차이가 **0~3600초(양 끝 포함)**일 때 수집한다. 따라서 5분/12분 지연으로 누락되지 않으며, 1시간을 넘으면 제외된다. 복원/대기 시간이 이 시각 판정에 영향을 줄 수 있다. dry-run/integration-test의 별도 합성 동작은 production에 적용되지 않는다.
- 이전 Snapshot은 Repository ID와 정확한 period.start로 조회한다. 이전 Snapshot의 measured_at과 현재 관측 시각도 각각 주차 경계 후 0~3600초여야 Star 비교에 사용한다. 없는 Snapshot이나 늦게 관측한 Snapshot은 추정으로 보완하지 않는다.
- Cold Start에서는 star_delta/star_growth/fork_delta/fork_growth가 unknown이다. Candidate 단계 discussion은 의도적으로 deep 단계까지 지연되어 discussion 비교도 대부분 unknown이다. Issue/PR와 이전 주 Issue/PR는 별도 GraphQL 집계이므로 Cold Start만으로 WoW가 반드시 unknown이 되는 것은 아니다.
- 첫 정규 실행도 Trending+activity/유의미한 release/security 등 서로 독립적인 근거를 갖춘 유효 저장소 5개가 없으면 실패할 수 있다. Trending 포함은 성공 보장이 아니다. 정량 수집이 처음 10개 및 Critical에 제한되는 점도 수집 가능한 근거 범위를 제한한다. 이번 점검에서는 이를 변경하지 않았다.
- 기존 실패 처리도 실제 관측 Snapshot/Metrics는 보존하며 report history/watchlist/Telegram 성공 이력은 만들지 않는다. 다음 주에 동일 저장소가 수집되고 두 측정이 경계 허용 범위에 있으면 실패 run의 관측값도 비교에 사용할 수 있다. 10월 16일에 관측한 10월 11일 주차 Snapshot은 다음 정규 실행의 올바른 경계 Snapshot을 대신할 수 없다.

## 진단 누락 원인과 수정

기존 Pipeline은 `candidate_diagnostics.json`과 logger에 상세 funnel을 남긴다. `configure_logging()`은 FileHandler만 등록하여 `logs/weekly_report.log`에 쓰고, One-shot Runner의 print/emit 경로는 `InsufficientCandidates` 이름만 출력했다. Workflow는 로그/DB artifact 업로드를 보안상 비활성화했으므로 Actions 콘솔에 상세 진단이 나타나지 않고 hosted runner 종료 후 로컬 자료도 사라진다. 기존 `automation_runs` 실패 기록에는 요약 오류만 저장되었다.

최소 수정:

1. One-shot Runner가 실패 예외의 진단에서 allowlist 집계만 콘솔에 출력한다. 후보/근거 본문/임의 오류 메시지/Secret은 포함하지 않는다. 진단 없는 구버전 예외는 UNAVAILABLE로 표시하며 수치를 추정하지 않는다.
2. Pipeline이 새 실패 run의 기존 `automation_runs.report` JSON 필드에 `candidate_diagnostics` 집계를 저장한다. schema/claim 정책은 그대로이며 실패 run의 기존 R2 업로드 경로로 영구 보존한다. 과거 run은 변경하지 않는다.
3. Data gaps에 previous_snapshot_missing/unusable, star_delta_unknown, star_growth_unknown, activity_unavailable을 집계한다. 상세 후보 진단의 잘못된 `star_growth_rate` 키를 실제 모델의 `star_growth`로 수정한다.

향후 Actions 콘솔에서 Candidate funnel → Exclusion reasons → Data gaps를 확인한다. 안전하게 별도 확보한 DB 사본에서는 `automation_runs.report`의 `candidate_diagnostics`를 읽을 수 있다. 과거 동일 주차 claim을 지우거나 재실행하여 진단을 재생성하지 않는다.

## 운영 판단

배포/실패 기록/R2 보존 경로는 사용자 결과 및 Mock 회귀 검증으로 정상임을 확인했다. 정규 실행은 retrospective 실행과 달리 1시간 안에 시작하면 live Trending을 사용할 수 있으므로 이번 실패만으로 정규 운영이 불가능하다고 판단하지 않는다. 반대로 Cold Start 및 근거 부족 위험이 남아 매주 TOP5 전송 성공을 보장하지 않는다.

수정본 validate 통과 후 실패 시에도 FAILED 기록과 진단을 확인하는 조건으로 schedule 활성화는 가능하다. 예약 활성화는 이번 작업에 포함하지 않는다. 초기 정규 실행은 1시간 경계, independent evidence 수, metric completed, Snapshot measured_at을 함께 관찰한다. 기존 10월 11일 종료 FAILED claim은 재실행하지 않는다.
