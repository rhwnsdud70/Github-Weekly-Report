# 구현 상태

- 마지막으로 완료된 Phase: 25
- 현재 진행 중인 Phase: 없음 — 코드/문서 및 Mock 검증 완료, 실제 운영 검증은 별도
- 완료된 기능: Phase 1~25 (아래 검증 기록 참조)
- 아직 구현하지 않은 기능: 요청한 Phase 1~25 코드에는 미완성 TODO 없음. 클린 설치 및 실 API 운영 검증은 미완료.
- 현재 실패 중인 테스트: 없음
- 알려진 문제: 기본 python/py 런처 미설정. 아래 실제 Python 경로 사용. 빈 `.venv`의 pip 설치는 패키지 인덱스 접근 불가로 실패; 설치 검증과 기존 환경 테스트 결과를 구분함.
- 다음 작업: 클린 환경 설치 및 실제 API 설정 후 운영 검증(현재 환경 제약은 문서 참조)
- 실행 방법: 실제 Python 경로로 `main.py run --dry-run --candidate-limit 10 --output artifacts/limited` 실행. API/발송 모두 Mock.
- 테스트 방법: `& 'C:/Users/고준영/.pyenv/pyenv-win/versions/3.11.9/python.exe' -m pytest -q`

## Phase 검증 기록

- Phase 1: 프로젝트 기본 구조; `1 passed in 0.04s`; 다음: Phase 2 설정 및 규칙

- Phase 2: 환경설정 및 중앙 규칙; `2 passed in 0.06s`; 다음: Phase 3 SQLite

- Phase 3: SQLite 스키마 및 CRUD 검증; `2 passed in 0.07s`; 다음: Phase 4 도메인 모델

- Phase 4: 도메인 모델 및 다중 사건 구조; `1 passed in 0.03s`; 다음: Phase 5 GitHub API 클라이언트

- Phase 5: 재사용 GitHub REST/GraphQL 클라이언트; `3 passed in 0.11s`; 다음: Phase 6 혼합 후보 수집

- Phase 6: Trending 검색 사건 혼합 수집; `2 passed in 0.11s`; 다음: Phase 7 전주 비교

- Phase 7: 정확한 직전 주 스냅샷 비교; `1 passed in 0.05s`; 다음: Phase 8 지표 계산

- Phase 8: 최근 주 활동 및 Release 지표; `1 passed in 0.15s`; 다음: Phase 9 점수 엔진

- Phase 9: 100점 엔진 및 모든 버킷 경계; `121 passed in 0.15s`; 다음: Phase 10 후보 검증

- Phase 10: 근거 부족 제외 및 Critical 단일 근거 예외; `2 passed in 0.11s`; 다음: Phase 11 중복/재선정

- Phase 11: Repository/사건 병합과 이전 소개 중복 방지; `2 passed in 0.05s`; 다음: Phase 12 TOP 5 선정

- Phase 12: 근거/이력 기반 TOP 5 및 다양성 비강제; `2 passed in 0.10s`; 다음: Phase 13 Critical 보안 강제 포함

- Phase 13: Critical 강제 포함 및 근거 기반 긴급도; `4 passed in 0.11s`; 다음: Phase 14 TOP 5 심층 수집

- Phase 14: 최종 TOP 5만 제한된 심층 수집; `1 passed in 0.09s`; 다음: Phase 15 출처 기반 근거 구조화

- Phase 15: 사실/의견 분리 및 원문 출처 검증; `1 passed in 0.03s`; 다음: Phase 16 LLM 서술

- Phase 16: 2단계 구조화 LLM 및 근거 검증/대체 서술; `2 passed in 0.09s`; 다음: Phase 17 Telegram 고정 포맷

- Phase 17: 고정 Telegram 레이아웃 및 보안 경고; `1 passed in 0.09s`; 다음: Phase 18 섹션 분할

- Phase 18: 섹션/문단 경계 분할 및 연속 순서; `2 passed in 0.05s`; 다음: Phase 19 Telegram 전송

- Phase 19: Telegram 최대 3회 시도와 실패 후 계속; `2 passed in 0.10s`; 다음: Phase 20 전송 로그

- Phase 20: 시도별 로그 및 PARTIAL_SUCCESS 판정; `1 passed in 0.11s`; 다음: Phase 21 스냅샷/이력 저장

- Phase 21: Snapshot/점수/TOP 5/소개 이력/Watchlist 저장; `1 passed in 0.11s`; 다음: Phase 22 스케줄러

- Phase 22: KST 일요일 19시 및 반개방 168시간 기간; `2 passed in 0.04s`; 다음: Phase 23 전체 Unit Test와 보완

- Phase 23: 전체 Unit Test 및 후보/Discussion/정책 회귀 검증; `158 passed in 0.34s`; 다음: Phase 24 CLI/Mock 통합/Dry Run

- Phase 24: 제한 Mock 통합/캐시 재사용/중복 발송 방지 및 40개 전체 Dry Run 1회 PASS; `10 passed in 0.70s`; 다음: Phase 25 README와 운영/검증 기록 완성

- Phase 25: README 설치/설정/실행/스케줄/복구 문서 및 CLI 명령 검증; `5 passed in 0.33s`; 다음: 클린 환경 설치 및 실제 API 설정 후 운영 검증(현재 환경 제약은 문서 참조)

## 설계 결정 / 미완성 코드

- 마스터 사양서가 최종 기준. API 실호출과 Telegram 실발송은 자격증명 미제공 시 Mock/Dry Run으로 검증.
- 재개 점검(2026-10-09): AGENTS 및 마스터 전체 확인, Git clean (87423d2), 전체 pytest **158 passed in 0.47s**. 기존 구현 재생성 없이 이어서 진행.
- 중단 파일 `database/db.py`, `models/serialization.py`, `services/persistence_service.py`, `services/pipeline.py`를 검토하고 기존 구현을 이어서 완성함.
- Phase 24 완료: 원자적 Persistence, 동시 주차 실행 claim, offset이 다른 동일 시각 중복 방지, serialization roundtrip, CLI, offline MockTransport. 제한 통합 테스트 통과(후보 10개, TOP 5, LLM 10회, 재검색 없는 캐시 재사용, 부분 전송 실패 후 재실행 차단).
- 전체 근거 부족 시 보고서/발송을 만들지 않고 실제 관측 Snapshot만 보관하는 실패 경로도 검증. 라이브 실행은 LLM/sender/전체 보고서 설정을 요구함.
- 최종 전체 pytest: **172 passed in 1.04s**. 이후 문서/CLI 관련 5개 테스트 통과. 현재 실패 테스트 없음.
- 후보 40개 전체 규모 Dry Run **1회 실행 완료**: Run `8fcf0516-11dc-4ec7-a6c0-c48dadf1a376`, SUCCESS. TOP 5, 심층 5개, LLM Mock 10회, Telegram Mock 8개, 최대 메시지 UTF-16 1,930 units. SQLite 사후 검사 PASS.
- DB 사후 검사: repositories 40, snapshots 80(전주 Fixture 40 포함), metrics 40, history 5, watchlist 3, transmission logs 8, automation_runs 1.
- Phase 25 완료: README 설치/키 설정/DB/수동 실행/테스트/스케줄/Dry Run/폴더/규칙/로그/복구 설명. `main.py --help`, `init-db`, 저장된 리포트의 `format` 명령 검증 완료.
- 산출물: `artifacts/full-dry-run/`, `artifacts/rendered-final/`; 상세 검증은 `docs/VALIDATION_REPORT.md`. 전체 규모 수집/생성은 다시 실행하지 말고 필요 시 기존 산출물만 검사.
- 설치 제약: `.venv` 생성 성공, pip 의존성 다운로드 실패(패키지 인덱스에서 배포판 조회 불가). 기존 설치 Python 환경의 테스트 성공을 클린 설치 성공으로 해석하지 않음.
- 운영 제약: 실 API Key/Telegram Destination 미제공으로 실호출·실발송 미검증. OS 스케줄 작업은 설치하지 않음. 실제 실행 중 비정상 종료된 live 주차는 자동 재발송하지 않고 점검하도록 문서화.
- 모든 코드와 문서는 실제 작업 트리에 저장됨. 이번 재개 작업의 Git commit은 생성하지 않음.
- 추가 회귀 수정: Major+Breaking Release의 최댓값 선택, 공개 High RCE의 Critical 예외, GitHub rename/transfer API redirect, 다음 주 Watchlist 재수집, 심층 원문 총량 제한.
- Star/Fork/Discussion Snapshot 비교는 실제 측정 시각이 각 주 종료 후 1시간 이내인 관측에 한정. 늦은 수동 실행에서 현재 수치를 과거 7일 변화로 해석하지 않음. 측정 시각과 정확한 이벤트 조사 기간은 별도 보관.
- 구조 선택: 모델은 `models/domain.py`, DB CRUD는 `database/db.py`에 응집. Issue+PR은 겹치지 않는 검색 결과의 합산. Fork/Discussion 정성 등급의 수치 보조 기준은 `config/rules.py`에 명시.
- Critical 저장소가 5개 초과인 경우 TOP 5와 전원 포함 규칙이 충돌하므로 실패 사유를 기록하고 임의 탈락시키지 않음.
