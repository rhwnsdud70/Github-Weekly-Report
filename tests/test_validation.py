from models.domain import Candidate, Repository, Metric, Event
from services.validation_service import validate_candidate


def test_insufficient_evidence_and_independent_signals():
    c = Candidate(Repository(1, 'a/b', 'url', stars=500))
    assert not validate_candidate(c)
    c.metrics.update(star_delta=Metric.known(200), star_growth=Metric.known(50))
    assert not validate_candidate(c)  # Same underlying star signal counts once.
    c.metrics['trending'] = Metric.known(True)
    assert validate_candidate(c)


def test_critical_single_evidence_exception():
    c = Candidate(Repository(1, 'a/b', 'url'), events=[Event('critical','advisory','url','date','security')])
    assert validate_candidate(c)
    c.events[0].source = 'issue'
    assert not validate_candidate(c)
