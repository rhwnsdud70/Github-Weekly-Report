"""Bounded, injectable GitHub transport; exceptions never include credentials."""
import time
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx


class GitHubError(RuntimeError):
    pass


class RateLimitError(GitHubError):
    pass


class ApiBudgetExceeded(GitHubError):
    pass


class GitHubValidationError(GitHubError):
    """A 422 response with a deliberately small, safe diagnostic payload."""
    pass


class GitHubClient:
    def __init__(self, token='', client=None, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=30)
        # Diagnostics only: never retain response bodies or request headers.
        self.last_status_code = None
        self.last_error_details = None
        self.headers = {'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28'}
        if token:
            self.headers['Authorization'] = f'Bearer {token}'
        self.sleep = sleep
        self._cache = {}
        self._budget_name = None
        self._budget_limit = None
        self._budget_used = 0
        self._budget_history = []
        self._endpoints = Counter()
        self._rest_requests = 0
        self._graphql_requests = 0
        self._rate_limits = {}
        self.last_graphql_errors = []

    def begin_budget(self, name, limit):
        if self._budget_name is not None:
            raise RuntimeError('A GitHub API budget is already active')
        self._budget_name, self._budget_limit, self._budget_used = name, limit, 0

    def end_budget(self):
        if self._budget_name is not None:
            self._budget_history.append({'name': self._budget_name, 'limit': self._budget_limit,
                                         'used': self._budget_used})
        self._budget_name, self._budget_limit = None, None

    def request_summary(self):
        """Safe request accounting; it deliberately contains no credentials or bodies."""
        search_requests = sum(count for endpoint, count in self._endpoints.items()
                              if endpoint.startswith('/search/'))
        return {'total_rest_requests': self._rest_requests, 'total_graphql_requests': self._graphql_requests,
                'search_rest_requests': search_requests,
                'endpoint_requests': dict(self._endpoints), 'rate_limits': dict(self._rate_limits),
                'budgets': list(self._budget_history),
                'active_budget': {'name': self._budget_name, 'limit': self._budget_limit, 'used': self._budget_used}}

    @staticmethod
    def _cache_key(path, params, body):
        return (path, tuple(sorted((params or {}).items())),
                json.dumps(body, sort_keys=True, separators=(',', ':')) if body is not None else None)

    def close(self):
        self.client.close()

    def request(self, path, params=None, body=None, _redirects=0, allow_graphql_errors=False):
        if _redirects == 0:
            self.last_error_details = None
        url = path if path.startswith('https://') else 'https://api.github.com/' + path.lstrip('/')
        if urlparse(url).netloc != 'api.github.com':
            raise GitHubError('Untrusted GitHub API host')
        key = self._cache_key(url, params, body)
        if _redirects == 0 and key in self._cache:
            return self._cache[key]
        if self._budget_limit is not None and self._budget_used >= self._budget_limit:
            raise ApiBudgetExceeded(f'GitHub {self._budget_name} API budget exhausted; source unavailable for this run')
        endpoint = '/graphql' if url.endswith('/graphql') else urlparse(url).path
        for attempt in range(3):
            try:
                self._budget_used += 1
                self._endpoints[endpoint] += 1
                if endpoint == '/graphql':
                    self._graphql_requests += 1
                else:
                    self._rest_requests += 1
                response = self.client.request('POST' if body is not None else 'GET', url,
                    params=params, json=body, headers=self.headers)
            except httpx.HTTPError:
                if attempt == 2:
                    raise GitHubError('GitHub transport failure') from None
                self.sleep(2 ** attempt)
                continue
            self.last_status_code = response.status_code
            self._record_rate_limit(response.headers)
            limited = response.status_code == 429 or (response.status_code == 403 and
                (response.headers.get('x-ratelimit-remaining') == '0' or
                 'retry-after' in response.headers or 'rate limit' in response.text.lower()))
            if limited:
                delay = response.headers.get('retry-after')
                suffix = ' (retry-after received)' if delay else ''
                raise RateLimitError('GitHub rate limit reached; source unavailable for this run' + suffix)
            remaining = response.headers.get('x-ratelimit-remaining')
            if remaining is not None and remaining.isdigit() and int(remaining) <= 5:
                raise RateLimitError('GitHub rate limit reserve reached; optional source unavailable for this run')
            if response.status_code in {301,302,307,308}:
                location = response.headers.get('location','')
                if _redirects >= 3 or not location.startswith('https://api.github.com/'):
                    raise GitHubError('Unsafe or excessive GitHub API redirect')
                return self.request(location,params,body,_redirects+1)
            if response.status_code >= 500 and attempt < 2:
                self.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                if response.status_code == 422:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    if isinstance(payload, dict):
                        self.last_error_details = {
                            key: payload[key] for key in ('message', 'errors', 'documentation_url')
                            if key in payload
                        }
                    raise GitHubValidationError('GitHub HTTP 422')
                raise GitHubError(f'GitHub HTTP {response.status_code}')
            try:
                data = response.json()
            except ValueError:
                raise GitHubError('GitHub invalid JSON') from None
            if isinstance(data, dict) and data.get('errors'):
                self.last_graphql_errors = [_safe_graphql_error(error) for error in data['errors']
                                            if isinstance(error, dict)]
                if allow_graphql_errors:
                    return data
                raise GitHubError('GitHub GraphQL API error')
            if _redirects == 0:
                self._cache[key] = data
            return data

    def _record_rate_limit(self, headers):
        resource = headers.get('x-ratelimit-resource', 'unknown')
        values = {key: headers.get('x-ratelimit-' + key) for key in ('limit', 'remaining', 'reset')}
        retry_after = headers.get('retry-after')
        if retry_after is not None:
            values['retry_after'] = retry_after
        if any(value is not None for value in values.values()):
            self._rate_limits[resource] = values

    def pages(self, path, params=None, max_pages=5):
        result = []
        for page in range(1, max_pages + 1):
            data = self.request(path, {**(params or {}), 'per_page': 100, 'page': page})
            items = data.get('items', []) if isinstance(data, dict) else data
            result.extend(items)
            if len(items) < 100:
                return result
        raise GitHubError('Pagination limit reached; incomplete data')

    def repository(self, name):
        return self.request(f'repos/{name}')

    def search(self, kind, query, limit=30, sort='updated'):
        data = self.request(f'search/{kind}', {'q': query, 'sort': sort, 'order': 'desc', 'per_page': limit})
        if data.get('incomplete_results'):
            raise GitHubError('Incomplete GitHub search results')
        return data

    def releases(self, name):
        return self.pages(f'repos/{name}/releases', max_pages=3)

    def releases_for_period(self, name, start, end, max_pages=3):
        """Return only releases in [start, end), stopping after older history."""
        result = []
        for page in range(1, max_pages + 1):
            items = self.request(f'repos/{name}/releases', {'per_page': 100, 'page': page})
            older_seen = False
            for item in items:
                published = item.get('published_at')
                if not published:
                    continue
                occurred_at = _github_instant(published)
                if start <= occurred_at < end:
                    result.append(item)
                elif occurred_at < start:
                    older_seen = True
            if older_seen or len(items) < 100:
                break
        return result

    def issues(self, name, query, limit=3):
        return self.search('issues', f'repo:{name} is:issue {query}', limit, 'comments')['items']

    def pulls(self, name, query, limit=2):
        return self.search('issues', f'repo:{name} is:pr {query}', limit, 'comments')['items']

    def discussions(self, name, limit=2):
        owner, repo = name.split('/')
        query = '''query($owner:String!,$name:String!,$limit:Int!){repository(owner:$owner,name:$name){
          discussions(first:$limit,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{
            title body url createdAt updatedAt author{login __typename}
            comments(first:10){nodes{body url createdAt author{login __typename}}}
          }}}}'''
        data = self.request('graphql', body={'query': query, 'variables': {
            'owner': owner, 'name': repo, 'limit': limit}})
        repository = data.get('data', {}).get('repository')
        if repository is None:
            raise GitHubError('Repository discussions unavailable')
        return repository['discussions']['nodes']

    def advisories(self, start, end):
        """List advisories published on inclusive calendar dates.

        The Global Advisory endpoint accepts GitHub search date ranges, not
        timezone-bearing ISO 8601 timestamp ranges. Exact weekly boundaries are
        enforced later with each advisory's published_at timestamp.
        """
        start_date = _advisory_date(start)
        end_date = _advisory_date(end)
        # A weekly report uses the first, newest page only. It is sorted so that
        # items outside the exact half-open period can be discarded by callers;
        # paging older results would waste the advisory budget.
        return self.request('advisories', {'published': f'{start_date}..{end_date}',
            'sort': 'published', 'direction': 'desc', 'per_page': 100})

    def activity_counts(self, names, start, end):
        """Fetch weekly Issue and PR counts for several repositories in one query.

        GitHub Search date qualifiers are calendar-date based and cannot express
        the KST 19:00 boundary. Fetch at most 100 lightweight `createdAt` nodes
        per alias, then apply the exact timezone-aware half-open comparison
        locally. A truncated alias is returned as None, never as a fabricated
        count.
        """
        names = list(dict.fromkeys(names))
        if not names:
            return {}
        if len(names) > 25:
            raise ValueError('GraphQL weekly activity batch is limited to 25 repositories')
        start_text = _search_timestamp(start)
        end_text = _search_timestamp(end-timedelta(seconds=1))
        self.last_graphql_errors = []
        variables, fields = {}, []
        for index, name in enumerate(names):
            for kind in ('issue', 'pr'):
                alias = f'r{index}_{kind}'
                variables[alias] = (f'repo:{name} is:{kind} '
                                    f'created:{start_text}..{end_text} sort:created-desc')
                fields.append(f'''{alias}: search(query:${alias}, type:ISSUE, first:100){{
                    issueCount
                    nodes{{... on Issue{{createdAt}} ... on PullRequest{{createdAt}}}}
                    pageInfo{{hasNextPage}}
                }}''')
        query = 'query(' + ','.join(f'${key}:String!' for key in variables) + '){' + ' '.join(fields) + '}'
        data = self.request('graphql', body={'query': query, 'variables': variables}, allow_graphql_errors=True)
        results = data.get('data', {})
        error_aliases = {_error_alias(error) for error in self.last_graphql_errors}
        global_error = None in error_aliases
        counts = {}
        for index, name in enumerate(names):
            try:
                counts[name] = {}
                for kind in ('issue', 'pr'):
                    alias = f'r{index}_{kind}'
                    connection = results.get(alias)
                    metric = 'issues' if kind == 'issue' else 'pulls'
                    if global_error or alias in error_aliases or connection is None:
                        counts[name][metric] = None
                    elif connection.get('issueCount') is not None:
                        counts[name][metric] = connection['issueCount']
                    else:
                        counts[name][metric] = sum(
                        start <= _github_instant(node['createdAt']) < end
                        for node in connection['nodes'])
            except (KeyError, TypeError):
                raise GitHubError('GraphQL weekly activity count unavailable') from None
        return counts

    def activity_count_fallback(self, name, kind, start, end):
        """Bounded REST fallback for one failed GraphQL alias."""
        query = (f'repo:{name} is:{"issue" if kind == "issues" else "pr"} '
                 f'created:{_search_timestamp(start)}..{_search_timestamp(end-timedelta(seconds=1))}')
        result = self.search('issues', query, 1, 'created')
        # Search total_count is the server-side count for the same exact UTC
        # timestamp range. Fetching every item only to count it wastes the
        # Search API budget and incorrectly made high-activity projects unknown.
        return result['total_count']

    def discussion_count(self, name):
        owner, repo = name.split('/')
        data = self.request('graphql', body={
            'query':'query($owner:String!,$name:String!){repository(owner:$owner,name:$name){discussions{totalCount}}}',
            'variables':{'owner':owner,'name':repo}})
        try:
            return data['data']['repository']['discussions']['totalCount']
        except (KeyError, TypeError):
            raise GitHubError('Discussion count unavailable') from None


def _advisory_date(value):
    if hasattr(value, 'date'):
        return value.date().isoformat()
    text = str(value)
    return text.split('T', 1)[0]


def _github_instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('GitHub timestamp must include a timezone')
    return result


def _search_timestamp(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _safe_graphql_error(error):
    result = {key:error[key] for key in ('message', 'path') if key in error}
    extensions = error.get('extensions') or {}
    code = extensions.get('type') or extensions.get('code')
    if code:
        result['code'] = code
    return result


def _error_alias(error):
    return next((part for part in error.get('path', [])
                 if isinstance(part, str) and part.startswith('r')), None)
