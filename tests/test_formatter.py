from models.domain import WeeklyReport
from services.score_service import score_candidate
from services.narrative_service import NarrativeService
from services.evidence_service import structure_evidence
from telegram.formatter import format_report
from tests.test_ranking import sample_candidates, period


def sample_report():
    top = sample_candidates(5)
    for c in top:
        score_candidate(c)
        structure_evidence(c, [])
        NarrativeService().generate(c)
    return WeeklyReport(period(), top)


def test_fixed_order_sections_and_unknown():
    messages = format_report(sample_report())
    assert [m.kind for m in messages] == ['summary', *(['top']*5), 'trends']
    for message in messages[1:6]:
        for section in ['주요 지표','GitHub']:
            assert section in message.text
        for empty_section in ['무엇을 하는','개발자에게','긍정적인 의견','논쟁','주의할 점']:
            assert empty_section not in message.text
        assert '해당 기간 데이터 미확보' in message.text


def test_optional_integration_banner_is_on_every_message():
    messages = format_report(sample_report(),banner='[INTEGRATION TEST]')
    assert messages
    assert all(message.text.startswith('[INTEGRATION TEST]') for message in messages)


def test_fixture_metric_note_requires_explicit_integration_output():
    from telegram.formatter import FIXTURE_METRIC_NOTE
    report = sample_report()
    for candidate in report.top:
        candidate.collection_sources.add('integration-test-fixture')
    integration = format_report(report,banner='[INTEGRATION TEST]')
    assert all(FIXTURE_METRIC_NOTE in message.text for message in integration if message.kind == 'top')
    assert all(FIXTURE_METRIC_NOTE not in message.text for message in format_report(report))


def test_reliability_summarizes_debug_but_preserves_material_failures():
    from telegram.formatter import REPRESENTATIVE_NOTE
    report = sample_report()
    for candidate in report.top:
        name = candidate.repository.full_name
        candidate.evidence['documents'] = [{'source_type':'README'}]
        candidate.evidence['validation_diagnostics'] = {'collection':{'source_failures':[
            {'source':'official docs','http_status':404,'optional':True}]}}
        report.reliability.extend([
            f'{name}: deep official docs: GitHubError',
            f'{name}: deep evidence limited to configured text budget; partial document coverage'])
    report.reliability.append('owner/repo: narrative degraded: NarrativeError')
    messages = format_report(report)
    text = '\n'.join(message.text for message in messages)
    assert 'deep official docs' not in text
    assert 'deep evidence limited' not in text
    assert text.count(REPRESENTATIVE_NOTE) == 1
    assert 'narrative degraded' in text
    report.top[0].evidence['documents'] = []
    assert '프로젝트 설명 근거가 부족할 수 있습니다' in format_report(report)[-1].text


def test_weekly_aggregate_trends_only_appear_in_summary():
    report = sample_report()
    messages = format_report(report)
    assert '선정 프로젝트 분야:' in messages[0].text
    assert '주요 Release' in messages[0].text
    assert '선정 프로젝트 분야:' not in messages[-1].text
    assert '선정 프로젝트 중 Trending' not in messages[-1].text
    assert '다음 주 관찰 프로젝트' in messages[-1].text
