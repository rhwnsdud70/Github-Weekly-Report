# GitHub Weekly AI & Developer Issues Automation

## Project Source of Truth

`docs/GitHub_주간_AI_개발_이슈_자동화_마스터_사양서_v1.0.txt`

위 문서를 이 프로젝트의 최상위 요구사항이자 Source of Truth로 취급한다.

구현 내용과 마스터 사양서가 충돌하는 경우 반드시 마스터 사양서를 우선한다.

마스터 사양서의 규칙을 임의로 축약하거나 변경하지 않는다.

초기 아이디어보다 마스터 사양서에 기록된 최종 확정 규칙을 우선한다.

---

# Development Goal

매주 일요일 19:00 KST에 지난 7일 동안 GitHub에서 화제가 된 AI 및 소프트웨어 개발 관련 프로젝트와 사건을 수집하고 분석한다.

최종적으로 서로 다른 프로젝트 기준 TOP 5를 선정하고, 근거 기반의 상세 분석을 생성하여 Telegram으로 전송하는 자동화 시스템을 구현한다.

핵심 목표:

- 최근 GitHub 활동 기반 후보 수집
- 이전 주 Snapshot 비교
- 100점 화제성 점수 계산
- 중복 프로젝트 및 다중 사건 통합
- Critical Security 예외 처리
- TOP 5 선정
- TOP 5만 심층 GitHub 분석
- 사실과 의견 분리
- LLM 기반 서술 생성
- Telegram 메시지 생성 및 분할
- Telegram Bot 전송
- 전송 실패 재시도
- 다음 주 분석을 위한 Snapshot 및 이력 저장
- 단계별 테스트

---

# Technology Stack

기본 구현 기술은 다음을 사용한다.

- Language: Python
- Database: SQLite
- GitHub Data: GitHub API 및 필요한 공개 GitHub 데이터
- AI: OpenAI API
- Telegram: Telegram Bot API
- Configuration: `.env`
- Tests: pytest
- Scheduling: APScheduler 또는 운영체제 scheduler 중 프로젝트에 적합한 방식
- Logging: Python logging

불필요하게 복잡한 프레임워크를 도입하지 않는다.

초기 구현은 단일 서버에서 실행되는 주간 Batch Automation을 기준으로 한다.

---

# Project Structure

가능하면 다음 구조를 기준으로 구현한다.

```text
github-weekly-report/
│
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── AGENTS.md
│
├── docs/
│   └── GitHub_주간_AI_개발_이슈_자동화_마스터_사양서_v1.0.txt
│
├── config/
│   ├── __init__.py
│   ├── settings.py
│   └── rules.py
│
├── collectors/
│   ├── __init__.py
│   ├── github_client.py
│   ├── github_trending.py
│   ├── github_search.py
│   ├── github_repository.py
│   ├── github_issue.py
│   ├── github_pr.py
│   ├── github_release.py
│   ├── github_discussion.py
│   └── github_security.py
│
├── models/
│   ├── __init__.py
│   ├── repository.py
│   ├── candidate.py
│   ├── score.py
│   ├── snapshot.py
│   └── report.py
│
├── database/
│   ├── __init__.py
│   ├── db.py
│   ├── schema.sql
│   ├── repository_store.py
│   ├── snapshot_store.py
│   ├── history_store.py
│   ├── watchlist_store.py
│   └── transmission_log_store.py
│
├── services/
│   ├── __init__.py
│   ├── candidate_service.py
│   ├── validation_service.py
│   ├── score_service.py
│   ├── duplicate_service.py
│   ├── ranking_service.py
│   ├── history_service.py
│   ├── deep_analysis_service.py
│   └── narrative_service.py
│
├── telegram/
│   ├── __init__.py
│   ├── formatter.py
│   ├── splitter.py
│   └── sender.py
│
├── prompts/
│   ├── evidence_analysis_prompt.txt
│   ├── narrative_prompt.txt
│   ├── community_analysis_prompt.txt
│   └── weekly_summary_prompt.txt
│
├── tests/
│   ├── test_scoring.py
│   ├── test_ranking.py
│   ├── test_duplicate.py
│   ├── test_validation.py
│   ├── test_formatter.py
│   └── fixtures/
│
├── data/
└── logs/
```

합리적인 이유가 있으면 세부 구조를 개선할 수 있지만, 역할 분리는 유지한다.

---

# Main Architecture Principle

`main.py`에 비즈니스 로직을 몰아넣지 않는다.

`main.py`는 전체 실행 순서를 조정하는 Orchestrator 역할만 한다.

대략적인 실행 순서는 다음과 같다.

```text
Report Period 결정
→ Previous Snapshot 로드
→ GitHub 후보 수집
→ 후보 병합 및 Repository ID 기준 중복 제거
→ 이전 주 데이터 매칭
→ Metric 계산
→ 데이터 검증
→ 화제성 점수 계산
→ TOP 5 후보 선정
→ 동일 프로젝트 다중 사건 통합
→ Critical Security 예외 처리
→ 최종 TOP 5
→ TOP 5 심층 GitHub 데이터 수집
→ 사실 / 의견 구조화
→ LLM 서술 생성
→ Telegram Format 생성
→ 긴 메시지 분할
→ Telegram 순차 전송
→ Snapshot / History / Watchlist / Log 저장
```

---

# GitHub Candidate Collection

후보 수집 전략은 마스터 사양서의 혼합형 방식을 따른다.

초기 목표 비중:

- GitHub Trending: 40%
- GitHub 전체 조건 검색: 40%
- Issue / Release / Security 이벤트: 20%

이 비율은 정확한 할당량이 아니라 후보 발굴 전략이다.

후보 전체를 LLM으로 심층 분석하지 않는다.

권장 흐름:

```text
초기 후보 약 30~50개
→ 정량 지표 수집
→ Score 계산
→ 상위 약 8~10개 검증
→ 최종 TOP 5
→ TOP 5만 심층 분석
```

토큰과 API 사용량을 불필요하게 증가시키지 않는다.

---

# Repository Identity

동일 프로젝트 식별은 반드시 다음 우선순위를 사용한다.

1. GitHub Repository ID
2. Canonical Repository URL
3. Owner + Repository Name

Repository 이름만으로 중복 여부를 판단하지 않는다.

Repository rename 또는 organization transfer에도 가능한 한 동일 프로젝트를 추적한다.

---

# Historical Data

매주 다음 데이터를 저장한다.

- 지난주 TOP 5
- 이전 화제성 점수
- 이전 Star
- 이전에 소개한 프로젝트 이력
- 다음 주 Watchlist

Star 증가량은 가능하면:

```text
현재 Snapshot Star - 직전 Snapshot Star
```

방식으로 계산한다.

외부에서 정확한 최근 7일 Star 증가량을 얻지 못하면 임의 추정하지 않는다.

---

# Unknown Data Policy

확인할 수 없는 값과 실제 `0`은 반드시 구분한다.

잘못된 방식:

```python
star_delta = 0
```

실제로 데이터가 확인되지 않은 것이라면:

```python
star_delta = None
star_delta_status = "unknown"
```

등으로 별도 상태를 저장한다.

허용 상태 예:

```text
confirmed
unknown
unavailable
not_applicable
```

확인되지 않은 값을 임의로 추정하거나 생성하지 않는다.

---

# Scoring Rules

화제성 총점은 100점이다.

- 7일 Star 증가량: 25
- 7일 Star 증가율: 20
- GitHub Trending: 15
- Issue / PR 활동: 15
- Fork / Discussions: 5
- Major Release: 10
- Special Event: 10

구체적인 Bucket 기준은 마스터 사양서와 `config/rules.py`에서 관리한다.

점수 기준 숫자를 여러 파일에 중복 하드코딩하지 않는다.

모든 주요 기준값은 가능한 한 `config/rules.py`에 둔다.

Score 결과는 총점뿐 아니라 세부 점수를 저장한다.

예:

```text
score_star_delta
score_growth_rate
score_trending
score_activity
score_community
score_release
score_special
total_score
```

---

# Duplicate Event Rules

동일 Repository가 여러 사건으로 여러 후보에 등장해도 최종 TOP 5에서는 한 번만 등장한다.

예:

```text
Project A / Event A → TOP 3
Project A / Event B → TOP 5
```

처리:

```text
Project A → TOP 3 유지
Event A + Event B 모두 TOP 3 분석에 포함
```

점수는 단순 합산하지 않는다.

여러 사건 중 가장 높은 대표 점수를 사용하고, 복합적인 영향은 Narrative에서 설명한다.

중복 통합으로 빈 순위가 발생하면 다음 후보를 승격한다.

---

# Category Diversity Rule

TOP 5의 분야 다양성을 인위적으로 맞추지 않는다.

TOP 5가 모두 AI Agent 등 동일 분야여도 실제 화제성 결과라면 그대로 유지한다.

분야 다양성을 위해 낮은 점수의 후보를 억지로 승격하지 않는다.

특정 분야가 TOP 5를 독점하면 그 현상 자체를 Weekly Trend에 반영한다.

---

# Critical Security Rule

Critical Security Issue는 최종 TOP 5 안에 반드시 포함한다.

대표 예:

- Critical vulnerability
- RCE
- Supply-chain attack
- Authentication bypass
- Large credential exposure
- 매우 광범위한 Critical security issue

Critical이라고 무조건 TOP 1로 배치하지는 않는다.

다음 요소를 종합해 순위를 결정한다.

- 영향 프로젝트 규모
- 영향 사용자 범위
- 공격 가능성
- 실제 악용 여부
- 패치 여부
- 생태계 영향
- 화제성 점수
- 긴급성

---

# Previous Report Duplicate Prevention

이전에 소개된 프로젝트는 단순히 점수가 높다는 이유만으로 반복 선정하지 않는다.

다음과 같은 실제 신규 변화가 있어야 재선정 가능하다.

- Major Release
- v1.0 stable release
- 새로운 Star 급증
- Critical / High Security Event
- License Change
- 중요한 API Change / Deprecation
- Project Shutdown
- Maintenance Stop
- Breaking Change
- 새로운 기술적 논쟁
- 개발 생태계에 영향을 미치는 새로운 사건

재선정 시 반드시:

```text
지난번 소개 이후 무엇이 달라졌는가
```

를 Narrative에 포함한다.

---

# Evidence Priority

서술 생성 시 근거 우선순위:

1. GitHub README / 공식 문서
2. Release Notes / Changelog
3. Issues
4. Discussions
5. Pull Requests
6. Security Advisory

외부 기사, 블로그, SNS는 보조 자료로만 사용한다.

---

# Fact and Opinion Separation

LLM 입력 전에 가능하면 데이터를 다음처럼 구조화한다.

```json
{
  "facts": [],
  "positive_opinions": [],
  "concerns": [],
  "debates": [],
  "risks": [],
  "reason_for_trending": [],
  "developer_impact": []
}
```

사실과 커뮤니티 의견을 섞지 않는다.

하나 또는 몇 개의 댓글을 커뮤니티 전체 의견처럼 표현하지 않는다.

---

# Community Opinion Generalization

여러 독립적인 Discussion / Issue에서 반복:
- "여러 개발자들이..."
- "반복적으로 제기되고 있다..."

하나의 Discussion에서 여러 사용자가 반복:
- "해당 Discussion에서는..."

일부 사용자:
- "일부 개발자는..."
- "몇몇 사용자는..."

한두 개 의견:
- "한 사용자 또는 일부 사용자가..."

근거가 충분하지 않은 경우:

- 대부분의 개발자
- 모든 개발자
- 커뮤니티 전체
- 압도적인 반응

등의 표현을 사용하지 않는다.

---

# LLM Usage

LLM을 Candidate Search Engine처럼 사용하지 않는다.

LLM은 최종 TOP 5의 근거 데이터를 받은 뒤 다음 작업에 집중한다.

- 사실 구조화
- 커뮤니티 의견 군집화
- 왜 화제가 되었는지 설명
- 프로젝트 설명
- 개발자 영향 설명
- 긍정 / 우려 / 논쟁 정리
- 주의사항 작성
- Weekly Trend Summary

가능하면:

```text
Evidence Extraction
→ Structured Analysis
→ Narrative Generation
```

두 단계 이상으로 나눈다.

LLM이 존재하지 않는 숫자나 사건을 생성하지 못하도록 입력 데이터 기반으로만 작성하게 한다.

---

# Telegram Formatting

Telegram의 전체 레이아웃을 LLM에게 맡기지 않는다.

LLM은 콘텐츠를 생성하고 Python Formatter가 고정된 메시지 형식을 만든다.

기본 순서:

1. Weekly Summary
2. TOP 1
3. TOP 2
4. TOP 3
5. TOP 4
6. TOP 5
7. Special Alert — 필요할 때만
8. Weekly Trends + Next Week Watchlist

TOP 1~5는 동일한 구조를 유지한다.

---

# Telegram Splitting

너무 긴 메시지는 문자 위치로 강제 절단하지 않는다.

금지:

```python
message[:4000]
```

권장:

```text
Section 단위 분할
```

예:

```text
TOP 1 (1/2)
- 주요 지표
- 왜 화제가 되었나
- 프로젝트 설명

TOP 1 (2/2)
- 개발자 중요성
- 커뮤니티 의견
- 주의사항
- 링크
```

문장 중간 또는 문단 중간에서 자르지 않는다.

---

# Telegram Retry Policy

각 메시지는 독립적으로 처리한다.

```text
최초 전송
→ 실패
→ 재시도 1
→ 실패
→ 재시도 2
→ 실패
→ 해당 메시지 최종 실패
→ 다음 메시지 계속
```

최대 총 시도 횟수는 3회이다.

하나의 메시지 실패 때문에 전체 Report 전송을 중단하지 않는다.

실패 메시지를 다음 주에 자동으로 다시 보내지 않는다.

---

# Telegram Link Preview

링크 미리보기는 기본 OFF로 한다.

GitHub Repository / Issue / Discussion / Release 링크는 필요한 것만 텍스트 링크로 제공한다.

---

# Secrets

비밀값을 절대로 Repository에 커밋하지 않는다.

`.env`는 Git에서 제외한다.

대신 `.env.example`을 작성한다.

예:

```env
GITHUB_TOKEN=
OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATABASE_PATH=data/github_weekly_report.db
TIMEZONE=Asia/Seoul
```

실제 Token이나 Secret 값을 생성하거나 임의로 작성하지 않는다.

---

# Database

초기 구현은 SQLite를 사용한다.

최소 다음 정보를 저장할 수 있어야 한다.

- repositories
- weekly_snapshots
- weekly_metrics
- report_history
- watchlist
- telegram_logs
- automation_runs

Database Schema 변경 시 기존 규칙에서 요구하는 데이터가 누락되지 않도록 한다.

---

# Automation Run Status

전체 실행 상태를 추적한다.

권장 상태:

```text
RUNNING
SUCCESS
PARTIAL_SUCCESS
FAILED
```

Telegram 일부 메시지만 실패했지만 분석 자체는 완료된 경우 `PARTIAL_SUCCESS`를 사용할 수 있다.

---

# Testing

각 핵심 단계 구현 후 테스트한다.

반드시 최소한 다음 테스트를 작성한다.

- Star Score 계산
- Growth Score 계산
- Trending Score 계산
- Issue / PR Score 계산
- Total Score
- Unknown 값 처리
- Repository 중복 통합
- 동일 프로젝트 다중 사건 통합
- Critical TOP 5 강제 포함
- Previous Report 중복 방지
- Telegram Message Splitting
- Telegram Retry Logic

테스트 실패 상태에서 다음 주요 단계로 넘어가지 않는다.

---

# Test Cost Control

전체 GitHub 검색 + LLM 분석을 반복적으로 실행하지 않는다.

개발 단계:

1. Fixture 기반 저비용 구조 테스트
2. 제한된 TOP 5 Narrative 테스트
3. Telegram Formatter 테스트
4. 마지막에 Full Integration Test 1회

문제가 있으면 해당 단계만 다시 테스트한다.

---

# Coding Style

- 작은 함수와 명확한 책임 분리를 선호한다.
- 불필요한 추상화는 피한다.
- 함수와 클래스 이름은 역할이 명확해야 한다.
- public 함수에는 필요한 경우 type hint를 사용한다.
- 데이터 구조는 가능하면 dataclass 또는 명확한 model로 정의한다.
- Network / Storage / Business Logic을 분리한다.
- 테스트 가능한 구조를 우선한다.
- 숫자 규칙은 config에서 관리한다.
- API 오류를 조용히 무시하지 않는다.
- Error와 Unknown Data를 구분한다.
- 로그에 Secret을 출력하지 않는다.

---

# Implementation Priority

전체 구현은 다음 순서로 진행한다.

1. Project Skeleton
2. Configuration
3. SQLite Database
4. Domain Models
5. GitHub Client
6. Repository / Candidate Collection
7. Historical Snapshot Comparison
8. Metric Calculation
9. Trending Score Engine
10. Candidate Validation
11. Duplicate / Previous Report Handling
12. TOP 5 Selection
13. Critical Security Handling
14. Deep Analysis Data Collection
15. Evidence Structuring
16. LLM Narrative Generation
17. Telegram Formatter
18. Telegram Message Splitter
19. Telegram Sender
20. Retry / Logging
21. Weekly Scheduler
22. Unit Tests
23. Integration Test
24. README / Setup Documentation

각 주요 단계에서 동작 확인 후 다음 단계로 진행한다.

---

# Final Principle

이 프로젝트의 최우선 순위는 다음과 같다.

1. 정확성
2. 근거성
3. 최근성
4. 개발자에게 실제로 중요한 정보
5. 화제성
6. 가독성
7. 비용 효율

데이터가 부족하면 임의로 채우지 않는다.

근거가 부족한 프로젝트는 후보에서 제외한다.

화려한 보고서보다 신뢰 가능한 보고서를 우선한다.