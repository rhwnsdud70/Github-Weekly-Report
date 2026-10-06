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
        for section in ['주요 지표','왜 화제가','무엇을 하는','개발자에게','긍정적인 의견','우려','논쟁','주의할 점','GitHub']:
            assert section in message.text
        assert '해당 기간 데이터 미확보' in message.text
