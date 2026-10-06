from datetime import datetime, timedelta, timezone
from models.domain import Candidate, Repository, Metric, ReportPeriod
from services.metric_service import calculate_metrics, growth


def test_activity_unknown_zero_and_growth():
    end = datetime(2026, 1, 11, tzinfo=timezone.utc)
    period = ReportPeriod(end-timedelta(days=7), end)
    candidate = Candidate(Repository(1, 'a/b', 'url', stars=500))
    calculate_metrics(candidate, period)
    assert candidate.metric('activity').status == 'unknown'
    candidate.metrics.update({k: Metric.known(v) for k, v in {
        'issues': 10, 'pulls': 30, 'previous_issues': 5, 'previous_pulls': 5}.items()})
    calculate_metrics(candidate, period)
    assert candidate.metric('activity').value == 40
    assert candidate.metric('activity_growth').value == 300
    assert 'activity' in candidate.signals
    assert growth(10, 0, '').status == 'not_applicable'
    assert growth(0, 10, '').value == -100
