from collections import Counter
from models.domain import TelegramMessage

UNKNOWN = '해당 기간 데이터 미확보'
ICONS = ('🥇','🥈','🥉','4️⃣','5️⃣')


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
    return [
        '선정 프로젝트 분야: ' + ', '.join(f'{name} {count}개' for name,count in categories.items()),
        f"선정 프로젝트에서 확인한 주요 Release {events['release']}건, 보안 사건 {events['security']}건",
        f"선정 프로젝트 중 Trending 진입 확인 {sum(c.metric('trending').value is True for c in report.top)}개",
    ]


def top_message(candidate, rank):
    repo, score, narrative = candidate.repository, candidate.score, candidate.narrative
    value = lambda name: candidate.metric(name).value
    trending = UNKNOWN if value('trending') is None else ('미진입' if not value('trending') else
        ('진입 확인 (관측 순위 ' + str(value('trending_rank')) + ')' if value('trending_rank') else '진입 확인 · 순위 미확인'))
    releases = [event.title for event in candidate.events if event.source == 'release']
    release_text = ', '.join(releases) or ('없음' if value('releases_checked') else UNKNOWN)
    header = f'{ICONS[rank-1]} TOP {rank} — {repo.full_name}'
    sections = [f'🤖 분야\n{repo.category}\n\n🔥 화제성 점수\n{score.total} / 100 — {score.grade}',
        '📈 이번 주 주요 지표\n' + '\n'.join([
            f'현재 Star: {display(repo.stars)}', f"최근 7일 Star 증가: {display(value('star_delta'))}",
            f"증가율: {display(value('star_growth'), '%')}", f'Trending: {trending}',
            f"Issue / PR: {display(value('issues'))} / {display(value('pulls'))}", f'Release: {release_text}']),
        section('💡 왜 화제가 되었나?', [section(label, narrative.get(key, [])) for key,label in [
            ('background','배경'),('trigger','직접적인 계기'),('attention','관심 증가'),('reaction','반응 이유')]]),
        section('🧩 무엇을 하는 프로젝트인가?', narrative.get('project', [])),
        section('👨‍💻 개발자에게 왜 중요한가?', narrative.get('impact', [])),
        section('💬 개발자들은 무엇을 이야기하고 있나?', [section(label,narrative.get(key, [])) for key,label in [
            ('positive','✅ 긍정적인 의견'),('concerns','⚠️ 우려하거나 비판하는 의견'),('debates','💭 논쟁 중인 부분')]]),
        section('🚨 주의할 점', narrative.get('risks', []))]
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


def format_report(report):
    trends = report.trends or weekly_trends(report)
    best = max(report.top, key=lambda c:c.score.total)
    messages = [TelegramMessage('summary','🔥 GitHub 주간 AI·개발 이슈',[
        f'📅 {report.period.start.isoformat()} 이상\n~ {report.period.end.isoformat()} 미만\n📊 TOP {len(report.top)}',
        section('📌 이번 주 핵심 흐름',trends[:3]),
        f'🏆 이번 주 최고 화제성 점수\n{best.repository.full_name} — {best.score.total}/100'])]
    messages.extend(top_message(candidate,rank) for rank,candidate in enumerate(report.top,1))
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
    messages.append(TelegramMessage('trends','📊 주간 전체 트렌드 + 다음 주 관찰',[
        section('수집·선정 범위에서 확인한 흐름', trends), section('다음 주 관찰 프로젝트',watch),
        '이번 주 한 줄 정리: ' + trends[0],
        section('데이터 신뢰성 안내', ['미확인 지표는 0이 아닌 미평가로 표시합니다. 표본의 반응을 커뮤니티 전체로 일반화하지 않습니다.',
            *report.reliability])]))
    return messages
