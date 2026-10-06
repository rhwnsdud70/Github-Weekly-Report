"""Bounded, injectable GitHub transport; exceptions never include credentials."""
import time
from urllib.parse import urlparse

import httpx


class GitHubError(RuntimeError):
    pass


class RateLimitError(GitHubError):
    pass


class GitHubClient:
    def __init__(self, token='', client=None, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=30)
        self.headers = {'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28'}
        if token:
            self.headers['Authorization'] = f'Bearer {token}'
        self.sleep = sleep

    def close(self):
        self.client.close()

    def request(self, path, params=None, body=None):
        url = path if path.startswith('https://') else 'https://api.github.com/' + path.lstrip('/')
        if urlparse(url).netloc != 'api.github.com':
            raise GitHubError('Untrusted GitHub API host')
        for attempt in range(3):
            try:
                response = self.client.request('POST' if body is not None else 'GET', url,
                    params=params, json=body, headers=self.headers)
            except httpx.HTTPError:
                if attempt == 2:
                    raise GitHubError('GitHub transport failure') from None
                self.sleep(2 ** attempt)
                continue
            limited = response.status_code == 429 or (response.status_code == 403 and
                (response.headers.get('x-ratelimit-remaining') == '0' or
                 'retry-after' in response.headers or 'rate limit' in response.text.lower()))
            if limited:
                raise RateLimitError('GitHub rate limit reached; source unavailable for this run')
            if response.status_code >= 500 and attempt < 2:
                self.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                raise GitHubError(f'GitHub HTTP {response.status_code}')
            try:
                data = response.json()
            except ValueError:
                raise GitHubError('GitHub invalid JSON') from None
            if isinstance(data, dict) and data.get('errors'):
                raise GitHubError('GitHub GraphQL API error')
            return data

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

    def issues(self, name, query, limit=3):
        return self.search('issues', f'repo:{name} is:issue {query}', limit, 'comments')['items']

    def pulls(self, name, query, limit=2):
        return self.search('issues', f'repo:{name} is:pr {query}', limit, 'comments')['items']

    def discussions(self, name, limit=2):
        owner, repo = name.split('/')
        query = '''query($owner:String!,$name:String!,$limit:Int!){repository(owner:$owner,name:$name){
          discussions(first:$limit,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{
            title body url createdAt updatedAt author{login}
            comments(first:10){nodes{body url createdAt author{login}}}
          }}}}'''
        data = self.request('graphql', body={'query': query, 'variables': {
            'owner': owner, 'name': repo, 'limit': limit}})
        repository = data.get('data', {}).get('repository')
        if repository is None:
            raise GitHubError('Repository discussions unavailable')
        return repository['discussions']['nodes']

    def advisories(self, start, end):
        return self.pages('advisories', {'published': f'{start}..{end}', 'type': 'reviewed'}, max_pages=10)

    def discussion_count(self, name):
        owner, repo = name.split('/')
        data = self.request('graphql', body={
            'query':'query($owner:String!,$name:String!){repository(owner:$owner,name:$name){discussions{totalCount}}}',
            'variables':{'owner':owner,'name':repo}})
        try:
            return data['data']['repository']['discussions']['totalCount']
        except (KeyError, TypeError):
            raise GitHubError('Discussion count unavailable') from None
