import pytest
from models.domain import Event, ScoreResult
from services.ranking_service import select_top, InsufficientCandidates
from tests.test_ranking import sample_candidates, period


def make_critical(candidate):
    candidate.events = [Event('critical','vulnerability','https://github.com/advisories/test',
                              period().start.isoformat(),'security')]
    candidate.score = ScoreResult({'special':10})
    candidate.metrics = {}


def test_critical_forced_not_automatically_first():
    candidates = sample_candidates()
    make_critical(candidates[-1])
    top, _ = select_top(candidates, period())
    assert candidates[-1].repository.identity in {c.repository.identity for c in top}
    assert not top[0].critical
    assert len({c.repository.identity for c in top}) == 5


def test_already_selected_once_and_overflow_conflict():
    candidates = sample_candidates()
    make_critical(candidates[0])
    top, _ = select_top(candidates + [candidates[0]], period())
    assert sum(c.critical for c in top) == 1
    for candidate in candidates:
        make_critical(candidate)
    with pytest.raises(InsufficientCandidates, match='conflict'):
        select_top(candidates, period())
