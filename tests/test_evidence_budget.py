from config import rules
from models.domain import Event
from services.deep_analysis_service import DeepAnalysisCollector
from tests.test_ranking import sample_candidates, period
from collectors.github_client import GitHubError


def test_text_budget_retains_security_and_release_evidence():
    class Unavailable:
        def __getattr__(self,name):
            def fail(*args,**kwargs):
                raise GitHubError('unavailable')
            return fail
    candidate = sample_candidates(1)[0]
    candidate.events = [Event('discussion','many comments',f'url{i}',period().start.isoformat(),
        'issue',{'body':'x'*10000}) for i in range(20)]
    candidate.events.append(Event('critical','security','advisory',period().start.isoformat(),
        'security',{'description':'security facts'}))
    documents = DeepAnalysisCollector(Unavailable()).collect(candidate,period())
    assert sum(len(d['text']) for d in documents) <= rules.DEEP_TOTAL_CHAR_LIMIT
    assert any(d['kind']=='security' for d in documents)
    assert candidate.errors
