import base64
import pytest
from collectors.github_client import GitHubError
from services.deep_analysis_service import DeepAnalysisCollector
from tests.test_ranking import sample_candidates, period


def test_only_top_and_bounded_collection():
    class Fake:
        def request(self, path, *args):
            if path.endswith('readme'):
                return {'content':base64.b64encode(b'Official README').decode(), 'html_url':'https://github.com/a/b'}
            raise GitHubError('unavailable')
        def issues(self, name, query, limit):
            assert limit == 3
            return []
        def pulls(self, name, query, limit):
            assert limit == 2
            return []
        def discussions(self, name, limit):
            assert limit == 2
            return []
    collector = DeepAnalysisCollector(Fake())
    with pytest.raises(ValueError):
        collector.collect_top(sample_candidates(6), period())
    documents = collector.collect_top(sample_candidates(5), period())
    assert len(documents) == 5
    assert all(len(items) == 1 for items in documents.values())
