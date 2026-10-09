import base64
from config import rules
from collectors.github_client import GitHubError
from services.metric_service import query_window


class DeepAnalysisCollector:
    def __init__(self, github):
        self.github = github

    def collect(self, candidate, period):
        name = candidate.repository.full_name
        documents = []
        def append(kind, url, title, text, author=None, occurred_at=None):
            if text:
                documents.append({'id': f'd{len(documents)+1}', 'kind': kind, 'url': url,
                    'title': title, 'text': text[:rules.DEEP_DOCUMENT_CHAR_LIMIT], 'author': author, 'occurred_at': occurred_at,
                    'truncated':len(text)>rules.DEEP_DOCUMENT_CHAR_LIMIT})
        def attempt(label, function):
            try:
                return function()
            except (GitHubError, ValueError, KeyError) as error:
                candidate.errors.append(f'deep {label}: {type(error).__name__}')
                return None
        readme = attempt('readme', lambda: self.github.request(f'repos/{name}/readme'))
        if readme:
            content = attempt('readme decode', lambda: base64.b64decode(readme['content']).decode('utf-8'))
            append('readme', readme.get('html_url', candidate.repository.url), 'README', content)
        docs = attempt('official docs', lambda: self.github.request(f'repos/{name}/contents/docs/index.md'))
        if docs and isinstance(docs, dict) and docs.get('content'):
            content = attempt('docs decode', lambda: base64.b64decode(docs['content']).decode('utf-8'))
            append('official_docs', docs.get('html_url', candidate.repository.url), 'Official documentation', content)
        for kind in ('issues', 'pulls'):
            items = attempt(kind, lambda: getattr(self.github, kind)(name, query_window(period), rules.DEEP_LIMITS[kind])) or []
            for item in items:
                if not period.contains(item['created_at']):
                    continue
                append(kind, item['html_url'], item['title'], item.get('body'),
                       (item.get('user') or {}).get('login'), item['created_at'])
                comments = attempt(kind + ' comments', lambda: self.github.request(
                    f"repos/{name}/issues/{item['number']}/comments", {'per_page': 10})) or []
                for comment in comments:
                    if period.contains(comment['created_at']):
                        append('comment', comment['html_url'], item['title'], comment.get('body'),
                               (comment.get('user') or {}).get('login'), comment['created_at'])
        discussions = attempt('discussions', lambda: self.github.discussions(name, rules.DEEP_LIMITS['discussions'])) or []
        for item in discussions:
            # An old discussion's newly updated body cannot prove a historical weekly event.
            if period.contains(item['createdAt']):
                append('discussions', item['url'], item['title'], item['body'],
                       (item.get('author') or {}).get('login'), item['createdAt'])
            for comment in item.get('comments', {}).get('nodes', []):
                if period.contains(comment['createdAt']):
                    append('comment', comment['url'], item['title'], comment['body'],
                           (comment.get('author') or {}).get('login'), comment['createdAt'])
        release_count = 0
        for event in candidate.events:
            if event.source == 'release':
                if release_count >= rules.DEEP_LIMITS['releases']:
                    continue
                release_count += 1
            append(event.source, event.url, event.title,
                event.details.get('body') or event.details.get('description') or event.title,
                occurred_at=event.occurred_at)
        # Keep official context and event evidence before filling the comment budget.
        prioritized = sorted(documents,key=lambda d:d['kind'] not in {'readme','official_docs','release','security'})
        bounded, used = [], 0
        for document in prioritized:
            size = len(document['text'])
            if used+size <= rules.DEEP_TOTAL_CHAR_LIMIT:
                bounded.append(document)
                used += size
        if len(bounded)<len(documents) or any(d['truncated'] for d in bounded):
            candidate.errors.append('deep evidence limited to configured text budget; partial document coverage')
        return bounded

    def collect_top(self, top, period):
        if len(top) > rules.TOP_COUNT:
            raise ValueError('Deep analysis restricted to final TOP 5')
        return {c.repository.identity: self.collect(c, period) for c in top}
