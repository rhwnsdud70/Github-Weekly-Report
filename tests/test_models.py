import pytest
from models.domain import Candidate, Event, Metric, Repository, ScoreResult


def test_identity_metrics_and_events():
    repo = Repository(1, 'a/b', 'https://github.com/a/b')
    renamed = Repository(1, 'c/d', 'https://github.com/c/d')
    assert repo.identity == renamed.identity
    assert Metric().value is None
    assert Metric.known(0).status == 'confirmed'
    with pytest.raises(ValueError):
        Metric(0)
    candidate = Candidate(repo, {'search', 'trending'}, [
        Event('major', 'v2', 'url1', '2026-01-01T00:00:00Z', 'release'),
        Event('critical', 'advisory', 'url2', '2026-01-01T00:00:00Z', 'security')])
    assert candidate.critical
    assert len(candidate.events) == 2
    assert ScoreResult({'a': 10, 'b': None}).total == 10
