from collections import Counter
from models.domain import TelegramMessage

UNKNOWN = '해당 기간 데이터 미확보'
ICONS = ('🥇','🥈','🥉','4️⃣','5️⃣')
FIXTURE_METRIC_NOTE = 'Star delta/growth, Fork/WoW comparison, historical Trending은 integration fixture 기반입니다.'
REPRESENTATIVE_NOTE = '일부 프로젝트는 대표 근거 문서만 선별해 분석했습니다.'


def display(value, suffix=''):
    if value is None:
        return UNKNOWN
    if isinstance(value, float):
        return f'{value:,.2f}{suffix}'
    return f'{value:,}{suffix}' if isinstance(value, int) else str(value) + suffix


def section(title, paragraphs):
    return title + '\n\n' + ('\n\n'.join(paragraphs) if paragraphs else '확인 가능한 근거 없음')


def weekly_trends(report):
    categories = Counter(c.repository.category for c in report.top)
    events = Counter(e.source for c in report.top for e in c.events)
    fixture_count = sum('INTEGRATION TEST FIXTURE' in c.metric('trending').source for c in report.top)
    trending_text = (f'Historical Trending: synthetic fixture for integration testing ({fixture_count}개)'
                     if fixture_count else
                     f"선정 프로젝트 중 Trending 진입 확인 {sum(c.metric('trending').value is True for c in report.top)}개")
    return [
        '선정 프로젝트 분야: ' + ', '.join(f'{name} {count}개' for name,count in categories.items()),
        f"선정 프로젝트에서 확인한 주요 Release {events['release']}건, 보안 사건 {events['security']}건",
        trending_text,
    ]


def top_message(candidate, rank, integration_test=False):
    repo, score, narrative = candidate.latest_repository or candidate.repository, candidate.score, candidate.narrative
    value = lambda name: candidate.metric(name).value
    trending = UNKNOWN if value('trending') is None else ('미진입' if not value('trending') else
        ('진입 확인 (관측 순위 ' + str(value('trending_rank')) + ')' if value('trending_rank') else '진입 확인 · 순위 미확인'))
    if integration_test and 'integration-test-fixture' in candidate.collection_sources:
        trending = 'synthetic fixture (순위 ' + str(value('trending_rank')) + ')'
    releases = [event.title for event in candidate.events if event.source == 'release']
    release_text = ', '.join(releases) or ('없음' if value('releases_checked') else UNKNOWN)
    header = f'{ICONS[rank-1]} TOP {rank} — {repo.full_name}'
    sections = [f'🤖 분야\n{repo.category}\n\n🔥 화제성 점수\n{score.total} / 100 — {score.grade}',
        '📈 이번 주 주요 지표\n' + '\n'.join([
            f'현재 Star: {display(repo.stars)}', f"최근 7일 Star 증가: {display(value('star_delta'))}",
            f"증가율: {display(value('star_growth'), '%')}", f'Trending: {trending}',
            f"Issue / PR: {display(value('issues'))} / {display(value('pulls'))}", f'Release: {release_text}'])]
    if candidate.observation.get('enabled'):
        current = ((candidate.observation.get('repository') or {}).get('current') or {}).get('snapshot') or {}
        observed_at = current.get('provenance',{}).get('observed_at') or UNKNOWN
        sections[1] += (f'\n최신 metadata 관측: {candidate.latest_observed_at or UNKNOWN}'
                        f'\n최신 Fork: {display(repo.forks)}'
                        f'\n평가 기준 Star: {display(candidate.repository.stars)} (관측: {observed_at})'
                        f'\n평가 기준 Fork: {display(candidate.repository.forks)}')
        page = candidate.observation.get('trending') or {}
        if value('trending') is True:
            sections[1] += '\nTrending 관측: '+str((page.get('provenance') or {}).get('observed_at') or UNKNOWN)
    if integration_test and 'integration-test-fixture' in candidate.collection_sources:
        sections[1] += '\n\n' + FIXTURE_METRIC_NOTE
    why = [section(label,narrative[key]) for key,label in [
        ('background','배경'),('trigger','직접적인 계기'),('attention','관심 증가'),('reaction','반응 이유')]
        if narrative.get(key)]
    if why:
        sections.append(section('💡 왜 화제가 되었나?',why))
    if narrative.get('project'):
        sections.append(section('🧩 무엇을 하는 프로젝트인가?',narrative['project']))
    if narrative.get('impact'):
        sections.append(section('👨‍💻 개발자에게 왜 중요한가?',narrative['impact']))
    community = [section(label,narrative[key]) for key,label in [
        ('positive','✅ 긍정적인 의견'),('concerns','⚠️ 우려하거나 비판하는 의견'),
        ('debates','💭 논쟁 중인 부분')] if narrative.get(key)]
    if community:
        sections.append(section('💬 개발자들은 무엇을 이야기하고 있나?',community))
    if narrative.get('risks'):
        sections.append(section('🚨 주의할 점',narrative['risks']))
    if candidate.changes_since_report:
        sections.append(section('지난번 소개 이후 무엇이 달라졌나', candidate.changes_since_report))
    links = candidate.evidence.get('important_links', [])
    link_sections = [f'🔗 GitHub\n{repo.url}']
    for kind, title in [('issues','🐛 주요 Issue'),('discussions','💬 주요 Discussion'),
                         ('release','📦 주요 Release'),('pulls','관련 PR'),('security','Security Advisory')]:
        urls = list(dict.fromkeys(link['url'] for link in links if link['kind'] == kind))[:3]
        if urls:
            link_sections.append(title + '\n' + '\n'.join(urls))
    sections.append('\n\n'.join(link_sections))
    return TelegramMessage('top', header, sections, rank)


def top1_test_message(candidate):
    """Format one artifact-backed TOP 1 test without fabricating empty sections."""
    narrative = candidate.narrative
    sections = []
    for title, key in [
        ('🧩 무엇을 하는 프로젝트인가?', 'project'),
        ('📈 확인된 관심 신호', 'attention'),
    ]:
        if narrative.get(key):
            sections.append(section(title, narrative[key]))
    events = [f'{event.title}\n{event.url}' for event in candidate.events]
    if events:
        sections.append(section('📌 주요 변화 / 이벤트', events[:3]))
    for title, key in [
        ('💬 커뮤니티 반응', 'reaction'),
        ('👨‍💻 개발자에게 왜 중요한가?', 'impact'),
        ('✅ 긍정적인 의견', 'positive'),
        ('⚠️ 우려 사항', 'concerns'),
        ('💭 논쟁 중인 부분', 'debates'),
        ('🚨 주의할 점', 'risks'),
    ]:
        if narrative.get(key):
            sections.append(section(title, narrative[key]))
    links = [candidate.repository.url]
    links.extend(link['url'] for link in candidate.evidence.get('important_links', [])
                 if link.get('kind') in {'release','issues','pulls','discussions','security'})
    sections.append('🔗 관련 GitHub 링크\n' + '\n'.join(dict.fromkeys(links)))
    header = f'[TEST] GitHub Weekly Report - TOP 1\n🥇 TOP 1 — {candidate.repository.full_name}'
    return TelegramMessage('telegram-test', header, sections, 1)


def telegram_reliability(report):
    """Keep operational detail in artifacts; surface meaningful coverage failures once."""
    result = []
    representative = False
    optional_unavailable = False
    for warning in report.reliability:
        if 'deep evidence limited to configured text budget; partial document coverage' in warning:
            representative = True
            continue
        if 'deep official docs:' in warning:
            name = warning.split(': deep official docs:',1)[0]
            candidate = next((c for c in report.top if c.repository.full_name == name),None)
            documents = candidate.evidence.get('documents',[]) if candidate else []
            fallback = any(d.get('source_type') in {'README','OfficialDocs'} for d in documents)
            failures = (candidate.evidence.get('validation_diagnostics',{}).get('collection',{})
                        .get('source_failures',[])) if candidate else []
            failure = next((f for f in failures if f.get('source') == 'official docs'),{})
            if fallback:
                # Missing optional conventional path is expected. Other failures still get a notice.
                if failure.get('http_status') != 404:
                    optional_unavailable = True
                continue
            warning = f'{name}: 공식 문서 수집이 제한되어 프로젝트 설명 근거가 부족할 수 있습니다.'
        if warning not in result:
            result.append(warning)
    if representative:
        result.append(REPRESENTATIVE_NOTE)
    if optional_unavailable:
        result.append('일부 추가 공식 문서를 가져오지 못해 README 등 확보된 근거로 분석했습니다.')
    return result


def format_report(report, banner=None):
    trends = report.trends or weekly_trends(report)
    best = max(report.top, key=lambda c:c.score.total)
    messages = [TelegramMessage('summary','🔥 GitHub 주간 AI·개발 이슈',[
        f'📅 {report.period.start.isoformat()} 이상\n~ {report.period.end.isoformat()} 미만\n📊 TOP {len(report.top)}',
        section('📌 이번 주 핵심 흐름',trends[:3]),
        f'🏆 이번 주 최고 화제성 점수\n{best.repository.full_name} — {best.score.total}/100'])]
    messages.extend(top_message(candidate,rank,integration_test=banner == '[INTEGRATION TEST]')
                    for rank,candidate in enumerate(report.top,1))
    for candidate in report.top:
        if candidate.critical:
            alerts = []
            for event in candidate.events:
                if event.source != 'security':
                    continue
                versions = event.details.get('vulnerabilities', [])
                affected = ', '.join(v.get('vulnerable_version_range','미확인') for v in versions) or '미확인'
                patched = ', '.join(str(v.get('first_patched_version') or '미확인') for v in versions) or '미확인'
                alerts.append(f'{event.title}\n영향 범위: {candidate.repository.full_name}\n영향 버전: {affected}\n'
                    f'패치 버전: {patched}\n긴급성: Critical 보안 검토 필요\n권장 조치: 공식 Advisory의 영향 버전과 수정 지침 확인\n{event.url}')
            messages.append(TelegramMessage('alert','🚨 특별 보안 경고',alerts))
    watch = [f'{c.repository.full_name} — {c.score.total}/100\n관찰 이유: {", ".join(sorted(c.signals))}\n'
             f'다음 주 확인: 성장 지속 및 신규 Release/보안 변화\n{c.repository.url}' for c in report.watchlist]
    messages.append(TelegramMessage('trends','📊 다음 주 관찰 + 신뢰성 안내',[
        section('다음 주 관찰 프로젝트',watch),
        section('데이터 신뢰성 안내', ['미확인 지표는 0이 아닌 미평가로 표시합니다. 표본의 반응을 커뮤니티 전체로 일반화하지 않습니다.',
            *telegram_reliability(report)])]))
    if banner:
        for message in messages:
            message.header = banner + '\n' + message.header
    return messages
