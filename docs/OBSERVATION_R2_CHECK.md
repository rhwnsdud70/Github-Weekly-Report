# Phase 7B — 격리된 R2 연결 검증

최초 구현은 Mock 검증만 수행했다. 이후 **2026-11-02 사용자가 실제 연결/인증200/Bucket200/PUT200/GET200/SHA/Conditional412/Cleanup204 성공을 보고**했다(Environment observation-r2-test, bucket gwr-observation-test). 이는 probe 작업의 실증이며 새 Pool/Boundary/Consumer 실연동은 NOT TESTED다. Codex에서 실제 호출/Dispatch/Push를 수행하지 않았다. 아래는 최초 probe의 사용 계약이며, suffix 없는 실제 bucket을 허용하는 새 경로는 [OBSERVATION_TEST_RUNTIME.md](OBSERVATION_TEST_RUNTIME.md)를 참조한다. 기존 probe 코드/Workflow는 그대로 유지했다.

## Cloudflare와 GitHub 설정

1. Cloudflare R2에서 **새 비공개 bucket** `gwr-observation-test-<unique-suffix>`를 생성한다. 소문자 영숫자·하이픈, 전체63자 이하, suffix 필요. 기존 운영 bucket을 이름만 바꾸거나 재사용하지 않는다.
2. R2 → API Tokens에서 **새 전용 Token/S3 Access Key**를 발급한다. Object Read & Write를 선택하고 **이번 테스트 bucket 하나에만** 권한을 제한한다. 운영 bucket 또는 전체 bucket 권한을 부여하지 않는다. 기존 Production token의 복사/재사용은 금지한다. probe에는 bucket 생성·삭제·설정 변경 권한이 필요 없다. Object List/Get/Put/Delete 권한을 확인한다. [Cloudflare 인증·권한 안내](https://developers.cloudflare.com/r2/api/tokens/).
3. 해당 bucket의 S3 endpoint를 확인한다. 표준 `https://<32자리 account ID>.r2.cloudflarestorage.com` 및 공식 jurisdiction별 `.eu`, `.us`, `.fedramp` endpoint를 허용한다. HTTPS만 허용하며 port/경로/query/사용자 인증정보/public r2.dev URL은 거부한다. endpoint는 연결 대상 설정이고 API token 자체가 아니다.
4. GitHub Repository → Settings → Environments에서 **`observation-r2-test`**를 만든다. 가능하면 Required reviewers/승인자와 배포 가능 branch 제한을 설정한다. 아래 네 값을 **Environment Secrets**로 등록한다. 실제 값은 코드·문서·콘솔에 복사하지 않는다.

| Environment Secret | 용도 |
|---|---|
| `OBS_R2_ENDPOINT` | 전용 테스트 bucket의 S3 endpoint |
| `OBS_R2_ACCESS_KEY_ID` | 새 전용 Access Key ID |
| `OBS_R2_SECRET_ACCESS_KEY` | 새 전용 Secret Access Key |
| `OBS_R2_BUCKET` | `gwr-observation-test-`로 시작하는 새 bucket 이름 |

`R2_*`는 읽지 않으며 Settings.load/.env도 사용하지 않는다. GitHub `secrets` context는 Environment 값이 없을 때 같은 이름의 Repository/Organization Secret을 찾을 수 있으므로 **OBS_R2_*를 Repository/Organization 범위에는 만들지 말고 Environment에 네 개 모두 등록**한다. 런타임에는 Secret의 발급 원본/실제 권한을 알아낼 수 없다. bucket prefix 검사만으로 credential이 운영 bucket에 접근 불가능하다고 증명할 수 없으므로 bucket 제한 권한은 사용자가 Cloudflare에서 확인해야 한다. 기존 Production R2/OpenAI/Telegram/GitHub Secret 이름·값·권한은 변경하지 않는다.

## 수동 실행

기본 실행은 완전 오프라인이다. Secret이 없어도 동작하며 SDK service model과 기존7A 구조 검증기만 사용한다. client/환경설정/SQLite는 열지 않는다.

```powershell
python scripts/check_observation_r2.py
python -m pytest tests/test_observation_r2_check.py -q
```

변경 검토·Push는 사용자가 별도로 수행한다. Actions → **Observation R2 Connectivity Check** → Run workflow → 먼저 `mode=validate`를 실행한다. validate는 Secret/Environment 없이 SDK·Observation 인터페이스 검사와 probe/Workflow Mock 테스트를 실행한다. 실패 코드는 그대로 Job 실패에 반영된다.

사용자가 테스트 bucket과 전용 권한을 확인한 후에만 `mode=live`, `confirm_isolated_bucket=true`로 실행한다. live job은 `observation-r2-test` Environment 승인을 거친다. 확인값 false는 Secret 주입/client 생성 전에 실패한다. schedule/push/pull_request 이벤트는 없다. 기존 Observation System validate/Weekly Workflow와 concurrency group이 다르며 probe 실행끼리는 `cancel-in-progress:false`, `queue:max`로 직렬화한다.

로컬 live도 명시적 옵션이 필요하다. 네 OBS 환경변수는 안전한 별도 방법으로 미리 주입하고 shell history/출력에 Secret을 적지 않는다. 운영 `.env`를 load하거나 Production R2 값을 복사하지 않는다. 다음은 사용자가 연결을 승인했을 때의 실행 예시일 뿐, 이번 작업에서 실행하지 않았다.

```powershell
python scripts/check_observation_r2.py --mode live --confirm-isolated-bucket --run-id 1 --attempt 1
```

## 검증 계약과 요청

기존 R2ObservationStore는 typed Observation/Pool envelope와 고정 namespace, canonical SHA-256, `IfNoneMatch='*'` PUT→GET verification을 사용한다. 이번 probe는 그 prefix를 확장하지 않으며 production store를 상속/호출하지 않는다. 동일 SDK 조건부 파라미터를 사용하는 별도 작은 service이며, 전체 Observation Envelope의 실 게시 검증은 아니다.

1. 네 OBS 설정, endpoint/bucket 및 명시적 확인을 검사한다. 실패하면 client를 만들지 않는다.
2. 설치된 botocore PutObject schema에 IfNoneMatch가 있는지 확인한다. 없으면 NOT_SUPPORTED 실패이며 일반 PUT으로 대체하지 않는다.
3. 전용 bucket의 정확한 probe prefix에 ListObjectsV2(MaxKeys=1)를 요청한다. 서명된 요청의 HTTP 성공으로 Authentication/Bucket Access를 검증한다. 계정 전체 ListBuckets/STS/Production DB 요청은 없다. List 결과는 권한 검사일 뿐 수집 목록이 아니므로 pagination을 순회하지 않는다.
4. 고유 `phase7b-probe/<run-id>/<attempt>/<random-uuid>/probe.json`에 `IfNoneMatch='*'`로 PUT한다. payload는 schema/purpose/random nonce뿐이다. UUID는 반복 실행 간 key 충돌을 피하며 한 실행의 timeout 재시도에서는 새 key를 만들지 않는다.
5. GET의 HTTP status, 제한된 Body(최대64KiB), ContentLength, exact bytes, SHA-256, `probe-id`/`sha256` metadata를 확인한다. ETag는 digest로 사용하지 않는다.
6. 같은 key에 다른 bytes로 두 번째 conditional PUT을 시도한다. **HTTP412**로 거절되고 원본 bytes/metadata를 GET으로 재확인해야 Conditional PUT SUCCESS다. 성공 응답으로 덮어쓰거나 다른 오류/timeout이면 실패한다. [Cloudflare S3 compatibility의 atomic conditional PUT 계약](https://developers.cloudflare.com/r2/api/s3/api/)을 실제 SDK로 실증하는 단계이며, 문서 지원과 실제 실행 성공은 구분한다.
7. 이번 실행이 소유한 nonce와 exact bytes를 GET으로 다시 확인한 뒤 정확한 key 하나만 DeleteObject한다. bucket/prefix 일괄 삭제 및 이전 실행 key 조회·삭제 기능은 없다. 초기412면 기존 객체로 취급해 삭제하지 않는다. 삭제 전 소유권 검사는 atomic conditional delete가 아니다. 무작위 key와 전용 bucket/직렬화가 전제이며 다른 writer가 동시에 probe key를 수정하게 해서는 안 된다. 불일치 시 삭제하지 않고 경고한다.

논리 SDK 호출 상한10회. 정상7회(List1+PUT2+GET3+Delete1), 첫 PUT lost-ack의 양성 GET 복구 시8회다. connect10초/read15초, SDK `total_max_attempts=1`로 숨은 retry 증폭을 막는다. 이는 SDK operation attempt 상한이며 DNS/TCP 패킷 수 계측은 아니다. Workflow timeout10분. client는 finally에서 닫는다. 실제 R2 조건부 쓰기/권한/물리 요청은 아직 미검증이다.

## 불확실성·오류·정리

PUT timeout/transport/5xx는 같은 key를 읽는다. 원본 bytes와 nonce가 일치하면 저장 사실을 확인할 수 있다. GET 실패/404는 진행 중 PUT이 나중에 commit하지 않는다는 증거가 아니므로 **UNCONFIRMED**, 성공 처리/새 key 재시도/불확실 객체 삭제를 하지 않는다. 다음 실행은 새 실행으로 구분되며 이전 객체를 정리하지 않는다.

두 번째 conditional PUT이 timeout/network/일부5xx로 미확인 상태라면 원본도 삭제하지 않는다. 진행 중 요청이 삭제 뒤에 새로 commit되는 상황을 피하기 위해 UNCONFIRMED/CLEANUP_WARNING으로 남기고 Lifecycle을 사용한다.

고정 category와 HTTP status만 출력하고 provider Error.Message/SDK exception/endpoint/bucket/key/credential/응답 본문은 출력하지 않는다. INVALID_ACCESS_KEY, SIGNATURE_MISMATCH, INVALID_CREDENTIALS, ACCESS_DENIED, BUCKET_NOT_FOUND, RATE_LIMIT, NETWORK_ERROR, TIMEOUT, OBJECT_NOT_FOUND, SHA256_MISMATCH, CONTENT_LENGTH_MISMATCH, OBJECT_METADATA_MISMATCH, CONDITIONAL_PUT_NOT_SUPPORTED, CONDITIONAL_PUT_OVERWRITE_NOT_BLOCKED 등을 구분한다. Signature mismatch는 secret 오류 외 signing/clock 문제도 가능하며 403만으로 자격증명 오류와 bucket 권한 오류를 확정하지 않는다. 인증/권한 오류는 빈 저장소로 처리하지 않는다.

정리 성공까지 exit0/SUCCESS. 검증 실패·미확인은 exit1/FAILED 또는 UNCONFIRMED. 검증이 성공했지만 정리 실패면 exit2/CLEANUP_WARNING이다. 원래 실패는 cleanup 성공으로 바뀌지 않는다. 민감 정보 없는 단계별 Summary만 GitHub Summary에 추가하고 artifact는 업로드하지 않는다. Production Database NOT ACCESSED / Claim NOT MODIFIED / Telegram NOT ACCESSED / Weekly Pipeline NOT EXECUTED를 명시한다.

Cloudflare Dashboard에서 **테스트 bucket에만** `phase7b-probe/` prefix 대상 1일 만료 Lifecycle을 별도로 설정하는 것을 권장한다. probe token에 bucket 설정 변경 권한을 추가하지 말고 관리자가 설정한다. 삭제 권한 부족/timeout/프로세스 강제 종료로 남은 객체는 이 규칙으로 만료시킨다. Lifecycle은 즉시 정리 보장이 아니며 지연될 수 있다. [Cloudflare Lifecycle 안내](https://developers.cloudflare.com/r2/buckets/object-lifecycles/).

테스트 종료 후 결과를 안전하게 기록하고 Cloudflare에서 **이번 전용 token만** 폐기하며 Environment의 OBS secrets를 삭제/교체한다. 기존 Production token을 폐기하지 않는다. 전용 bucket 수동 정리는 잔존 객체를 확인한 사용자가 별도로 수행한다.

## 다음 단계

사용자가 live Summary의 Authentication/Bucket/PUT/GET/SHA/Conditional/Cleanup 전부 SUCCESS를 확인해야 Phase7B 실제 실증 완료다. 현재는 **NOT TESTED**다. Phase7C는 Pool 발행 운영 주체/관측용 최소 권한·설정 분리/수동 Observation 수집 인터페이스 준비가 대상이다. 이번 probe의 전용 테스트 설정은 기존 Production Consumer loader 설정을 바꾸거나 실제 수집을 활성화하지 않는다. 정규 관측·Trending/GitHub/OpenAI/Telegram/기존 Claim/DB/Workflow에는 연결하지 않는다.
