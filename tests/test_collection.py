from datetime import datetime, timezone
from collectors.github_trending import TrendingParser
from collectors.github_release import release_event
from collectors.github_security import advisory_repository
from collectors.github_client import GitHubError
from models.domain import ReportPeriod
from services.candidate_service import CandidateCollector


def test_trending_rank_and_release():
    parser = TrendingParser()
    parser.feed('<h2><a href="/owner/a">a</a></h2><h2><a href="/owner/b">b</a></h2>')
    assert parser.repositories == ['owner/a', 'owner/b']
    assert release_event({'tag_name': 'v2.0.0', 'html_url': 'url', 'published_at': 'now'}).kind == 'major'
    assert advisory_repository({'source_code_location': 'https://example.org/a/b'}) is None


def test_source_failure_and_merge():
    class Fake:
        def repository(self, name):
            return {'id': 1, 'full_name': name, 'html_url': 'https://github.com/' + name, 'topics': ['llm']}
        def search(self, kind, query, *args):
            if kind == 'issues':
                return {'items': []}
            return {'items': [self.repository('a/b')]}
        def advisories(self, *args):
            return []
    def unavailable(_):
        raise GitHubError('Trending unavailable')
    collector = CandidateCollector(Fake(), None, unavailable)
    period = ReportPeriod(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 8, tzinfo=timezone.utc))
    result = collector.collect(period)
    assert len(result) == 1
    assert result[0].metric('trending').status == 'unknown'
    assert collector.errors
