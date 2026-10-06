import httpx
import pytest
from collectors.github_client import GitHubClient, GitHubError, RateLimitError


def test_errors_and_no_token_leak():
    client = GitHubClient('secret', httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, headers={'x-ratelimit-remaining': '0'}))))
    with pytest.raises(RateLimitError) as error:
        client.repository('a/b')
    assert 'secret' not in str(error.value)
    with pytest.raises(GitHubError, match='Untrusted'):
        client.request('https://evil.invalid/path')


def test_retry_and_search():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={'items': [], 'total_count': 0})
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(respond)), sleep=lambda _: None)
    assert client.search('repositories', 'topic:llm')['total_count'] == 0
    assert len(calls) == 2


def test_incomplete_results_not_zero():
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'items': [], 'incomplete_results': True}))))
    with pytest.raises(GitHubError, match='Incomplete'):
        client.search('issues', 'repo:a/b')
