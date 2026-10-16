from datetime import datetime, timezone
import json
import httpx
import pytest
from collectors.github_client import ApiBudgetExceeded, GitHubClient, GitHubError, RateLimitError


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
    assert client.last_status_code == 200


def test_incomplete_results_not_zero():
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'items': [], 'incomplete_results': True}))))
    with pytest.raises(GitHubError, match='Incomplete'):
        client.search('issues', 'repo:a/b')


def test_repository_transfer_redirect_and_untrusted_redirect():
    def respond(request):
        if request.url.path == '/repos/old/repo':
            return httpx.Response(301,headers={'location':'https://api.github.com/repositories/123'})
        return httpx.Response(200,json={'id':123,'full_name':'new/repo'})
    client = GitHubClient('fake',httpx.Client(transport=httpx.MockTransport(respond)))
    assert client.repository('old/repo')['id'] == 123
    client = GitHubClient('fake',httpx.Client(transport=httpx.MockTransport(
        lambda _:httpx.Response(301,headers={'location':'https://untrusted.invalid/'}))))
    with pytest.raises(GitHubError,match='Unsafe'):
        client.repository('old/repo')


def test_advisory_dates_are_calendar_ranges_without_type_parameter():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[])
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(respond)))
    client.advisories(datetime(2026, 1, 1, 19, tzinfo=timezone.utc),
                      datetime(2026, 1, 8, 19, tzinfo=timezone.utc))
    assert requests[0].url.params['published'] == '2026-01-01..2026-01-08'
    assert 'type' not in requests[0].url.params
    assert requests[0].url.params['sort'] == 'published'
    assert requests[0].url.params['direction'] == 'desc'
    assert requests[0].url.params['per_page'] == '100'
    assert len(requests) == 1


def test_activity_counts_batches_issue_and_pr_with_half_open_period():
    requests = []
    def respond(request):
        requests.append(request)
        payload = json.loads(request.content)
        boundary = ['2025-12-31T23:59:59+00:00', '2026-01-01T00:00:00+00:00',
                    '2026-01-07T23:59:59+00:00', '2026-01-08T00:00:00+00:00',
                    '2026-01-08T00:00:01+00:00']
        data = {}
        for alias in payload['variables']:
            inside = 12 if alias.endswith('_issue') else 7
            nodes = [{'createdAt': timestamp} for timestamp in boundary]
            nodes += [{'createdAt':'2026-01-02T00:00:00+00:00'} for _ in range(inside-2)]
            data[alias] = {'nodes':nodes, 'pageInfo':{'hasNextPage':False}}
        return httpx.Response(200, json={'data': data})
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(respond)))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 8, tzinfo=timezone.utc)
    counts = client.activity_counts(['a/one', 'b/two'], start, end)
    assert counts == {'a/one': {'issues': 12, 'pulls': 7}, 'b/two': {'issues': 12, 'pulls': 7}}
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert 'created:2026-01-01T00:00:00Z..2026-01-07T23:59:59Z sort:created-desc' in str(payload['variables'])
    assert client.request_summary()['total_graphql_requests'] == 1


def test_activity_batch_keeps_success_alias_when_pr_alias_has_graphql_error():
    def respond(request):
        payload = json.loads(request.content)
        data = {alias:{'issueCount':12, 'nodes':[], 'pageInfo':{'hasNextPage':True}}
                for alias in payload['variables']}
        data['r0_pr'] = None
        return httpx.Response(200, json={'data':data, 'errors':[{'message':'bad PR alias',
            'path':['r0_pr'], 'extensions':{'code':'SOMETHING'}}]})
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(respond)))
    result = client.activity_counts(['a/b'], datetime(2026,1,1,tzinfo=timezone.utc),
                                    datetime(2026,1,8,tzinfo=timezone.utc))
    assert result == {'a/b': {'issues':12, 'pulls':None}}
    assert client.last_graphql_errors == [{'message':'bad PR alias','path':['r0_pr'],'code':'SOMETHING'}]


def test_activity_rest_fallback_uses_exact_search_total_count_without_pagination():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'total_count':417, 'items':[], 'incomplete_results':False})
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(respond)))
    value = client.activity_count_fallback('a/b', 'pulls', datetime(2026,1,1,tzinfo=timezone.utc),
                                           datetime(2026,1,8,tzinfo=timezone.utc))
    assert value == 417
    assert requests[0].url.params['per_page'] == '1'
    assert 'repo:a/b is:pr created:2026-01-01T00:00:00Z..2026-01-07T23:59:59Z' in requests[0].url.params['q']


def test_release_period_filter_has_exact_boundaries_and_stops_at_old_page():
    calls = []
    timestamps = ['2026-01-08T00:00:01+00:00', '2026-01-08T00:00:00+00:00',
                  '2026-01-07T23:59:59+00:00', '2026-01-01T00:00:00+00:00',
                  '2025-12-31T23:59:59+00:00']
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=[{'published_at': stamp} for stamp in timestamps])
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(respond)))
    releases = client.releases_for_period('a/b', datetime(2026,1,1,tzinfo=timezone.utc),
                                          datetime(2026,1,8,tzinfo=timezone.utc))
    assert [item['published_at'] for item in releases] == [timestamps[2], timestamps[3]]
    assert len(calls) == 1


def test_422_keeps_only_safe_details():
    client = GitHubClient('secret', client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(422, json={'message':'Validation Failed','errors':[{'field':'published'}],
                                             'documentation_url':'https://docs.github.com/x','token':'secret'}))))
    with pytest.raises(GitHubError, match='422'):
        client.request('advisories', {'published':'bad'})
    assert client.last_error_details == {'message':'Validation Failed','errors':[{'field':'published'}],
                                         'documentation_url':'https://docs.github.com/x'}


def test_identical_requests_use_cache_and_are_counted_once():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 1}, headers={'x-ratelimit-resource':'core',
            'x-ratelimit-limit':'5000','x-ratelimit-remaining':'4999','x-ratelimit-reset':'123'})
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert client.repository('a/b')['id'] == 1
    assert client.repository('a/b')['id'] == 1
    assert len(calls) == 1
    summary = client.request_summary()
    assert summary['total_rest_requests'] == 1
    assert summary['endpoint_requests']['/repos/a/b'] == 1
    assert summary['rate_limits']['core']['remaining'] == '4999'


def test_budget_and_rate_limit_headers_stop_optional_requests():
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'id': 1}))))
    client.begin_budget('candidate', 1)
    client.repository('a/b')
    with pytest.raises(ApiBudgetExceeded, match='budget exhausted'):
        client.repository('c/d')
    client.end_budget()
    assert client.request_summary()['budgets'] == [{'name':'candidate','limit':1,'used':1}]
    limited = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(429, headers={'retry-after':'60','x-ratelimit-resource':'search',
            'x-ratelimit-limit':'30','x-ratelimit-remaining':'0','x-ratelimit-reset':'456'}))))
    with pytest.raises(RateLimitError, match='retry-after'):
        limited.search('issues','repo:a/b')
    assert limited.request_summary()['rate_limits']['search']['retry_after'] == '60'
