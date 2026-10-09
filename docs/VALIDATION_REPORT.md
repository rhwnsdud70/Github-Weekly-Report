# 검증 기록

2026-10-09 재개 시 기준 커밋: `87423d2`.

## 실행한 검증

| 검증 | 결과 |
|---|---|
| 재개 직후 기존 전체 테스트 | 158 passed |
| 구현 보완 후 전체 테스트 | 172 passed in 1.04s |
| 제한 통합 | 합성 후보 10개, 최종 TOP 5만 심층 분석, OpenAI Mock 응답 10회 |
| 비용 제어 | 후보 캐시 재사용 시 Repository 재검색 없음, report.json 재포맷 시 네트워크 호출 없음 |
| 전송 실패 | TOP 2가 3회 실패해도 TOP 5까지 계속, PARTIAL_SUCCESS |
| 같은 주차 재실행 | 재수집/LLM/Telegram 호출 전에 차단 |
| 동시 실행 | 두 DB 연결의 경쟁에서 한 실행만 주차 확보 |
| 저장 실패 | Watchlist 저장 실패 시 Snapshot/Metrics/History까지 원자적 롤백 |
| 근거 부족 | TOP 5 생성·LLM·전송 중단, 관측 Snapshot만 저장, FAILED 기록 |
| 전체 규모 Dry Run | 아래 실행 **1회**, SUCCESS 및 사후 검사 PASS |
| 빈 가상환경 설치 | 가상환경 생성 성공, pip 패키지 인덱스 접근 불가로 의존성 다운로드 실패 |

## 전체 규모 Dry Run

```powershell
& 'C:/Users/고준영/.pyenv/pyenv-win/versions/3.11.9/python.exe' main.py run --dry-run --candidate-limit 40 --at '2026-10-04T19:00:00+09:00' --output artifacts/full-dry-run
& 'C:/Users/고준영/.pyenv/pyenv-win/versions/3.11.9/python.exe' -m scripts.verify_run artifacts/full-dry-run
```

- Run ID: `8fcf0516-11dc-4ec7-a6c0-c48dadf1a376`
- 조사 기간: `2026-09-27 19:00 KST <= event < 2026-10-04 19:00 KST` (168시간)
- 후보 40개, 서로 다른 TOP 5, Critical 후보 포함
- 심층 분석 5개, OpenAI Mock 호출 10회, 메시지 8개
- 메시지 최대 길이: UTF-16 1,930 units (제한 4,096 이내)
- DB: repositories 40, snapshots 80 (전주 합성 기준 40 + 현재 40), metrics 40
- DB: report_history 5, watchlist 3, telegram_logs 8, automation_runs 1
- 실제 GitHub/OpenAI/Telegram 요청 및 비용: 없음
- `artifacts/full-dry-run/`에 report.json, candidates.json, messages.txt, SQLite, verification.json, verification_checks.json 저장. 생성 데이터는 Git에서 제외.

## 검증 범위와 한계

- 모든 테스트에서 실제 socket 연결을 차단하며 HTTP 응답은 MockTransport로 주입한다.
- Dry Run의 Repository, Event, 숫자, LLM 응답은 합성 Fixture다. 실제 GitHub 뉴스나 실제 LLM 서술 품질의 검증 결과가 아니다.
- 실제 API 자격증명과 Telegram 목적지는 제공되지 않았다. 실호출·실발송·운영 권한 검증은 수행하지 않았다.
- 현재 테스트는 사전 설치된 Python 3.11.9, httpx 0.28.1, python-dotenv 1.0.1, pytest 8.3.5 환경에서 실행했다. 빈 `.venv`의 설치 성공을 주장하지 않는다.
- 전체 규모 수집/생성을 반복하지 않았다. 이후 메시지 검사는 저장된 출력만 사용했다.
