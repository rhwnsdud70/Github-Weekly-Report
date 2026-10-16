from datetime import datetime, timedelta, timezone
from models.domain import Candidate, Repository, Metric, ReportPeriod
from services.metric_service import calculate_metrics, growth
from services.metric_service import collect_metrics, collect_metrics_batch


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


def test_previous_counts_use_snapshot_and_unavailable_current_is_not_zero():
    class LimitedGitHub:
        def activity_counts(self, *args):
            raise __import__('collectors.github_client', fromlist=['GitHubError']).GitHubError('rate limit')
        def releases(self, *args):
            raise __import__('collectors.github_client', fromlist=['GitHubError']).GitHubError('rate limit')
    end = datetime(2026, 1, 11, tzinfo=timezone.utc)
    candidate = Candidate(Repository(1, 'a/b', 'url', stars=500))
    candidate.previous = {'snapshot': {'issues': 7, 'pulls': 3}, 'snapshot_usable': True}
    collect_metrics(candidate, LimitedGitHub(), ReportPeriod(end-timedelta(days=7), end))
    assert candidate.metric('previous_issues').status == 'unavailable'
    assert candidate.metric('previous_pulls').status == 'unavailable'
    assert candidate.metric('issues').status == 'unavailable'
    assert candidate.metric('pulls').status == 'unavailable'


def test_partial_graphql_uses_rest_fallback_only_for_failed_metric():
    class Partial:
        def __init__(self): self.calls = []
        def activity_counts(self, names, start, end):
            return {names[0]: {'issues':12, 'pulls':None}}
        def activity_count_fallback(self, name, kind, start, end):
            self.calls.append((name, kind)); return 7
        def releases(self, *args): return []
    end = datetime(2026, 1, 11, tzinfo=timezone.utc)
    candidate = Candidate(Repository(1, 'a/b', 'url', stars=500))
    client = Partial()
    collect_metrics_batch([candidate], client, ReportPeriod(end-timedelta(days=7), end))
    assert candidate.metric('issues').value == 12
    assert candidate.metric('pulls').value == 7
    assert client.calls == [('a/b', 'pulls')]
