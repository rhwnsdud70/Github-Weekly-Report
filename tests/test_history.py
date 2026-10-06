from datetime import datetime, timedelta, timezone
from database.db import Database
from models.domain import Candidate, Repository, ReportPeriod
from services.history_service import attach_history


def test_exact_previous_week_and_rename():
    end = datetime(2026, 1, 11, 10, tzinfo=timezone.utc)
    period = ReportPeriod(end-timedelta(days=7), end)
    db = Database(':memory:')
    repo = Repository(1, 'renamed/repo', 'url', stars=150)
    db.repository(repo.identity, {'github_id': 1, 'full_name': 'old/repo', 'url': 'url'})
    c = Candidate(repo)
    attach_history(c, db, period)
    assert c.metric('star_delta').status == 'unknown'
    db.snapshot(repo.identity, period.start.isoformat(), {'measured_at': period.start.isoformat(), 'stars': 100})
    attach_history(c, db, period)
    assert c.metric('star_delta').value == 50
    assert c.metric('star_growth').value == 50
    c.repository.stars = 100
    attach_history(c, db, period)
    assert c.metric('star_delta').value == 0
    assert c.metric('star_delta').status == 'confirmed'
    attach_history(c, db, ReportPeriod(end, end+timedelta(days=7)))
    assert c.metric('star_delta').status == 'unknown'
