from datetime import datetime, timedelta, timezone
from models.domain import Candidate, Repository, Metric, ScoreResult, ReportPeriod
from services.ranking_service import select_top, InsufficientCandidates
import pytest


def sample_candidates(count=7):
    return [Candidate(Repository(i, f'owner/repo{i}', f'https://github.com/owner/repo{i}', category='AI', stars=1000),
        metrics={'trending':Metric.known(True),'star_delta':Metric.known(200)}, score=ScoreResult({'score':80-i})) for i in range(count)]


def period():
    end = datetime(2026,1,11,tzinfo=timezone.utc)
    return ReportPeriod(end-timedelta(days=7),end)


def test_no_category_quota_and_no_duplicate():
    candidates = sample_candidates()
    candidates[-1].repository.category = 'Database'
    top, watch = select_top(candidates + [candidates[0]], period())
    assert len({c.repository.identity for c in top}) == 5
    assert all(c.repository.category == 'AI' for c in top)
    assert len(watch) == 2


def test_insufficient_does_not_fabricate():
    with pytest.raises(InsufficientCandidates):
        select_top(sample_candidates(4), period())
