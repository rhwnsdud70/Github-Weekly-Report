import base64
import re
import logging
from config import rules
from collectors.github_client import GitHubError, RateLimitError, ApiBudgetExceeded
from services.metric_service import query_window

logger = logging.getLogger(__name__)


class DeepAnalysisCollector:
    def __init__(self, github):
        self.github = github

    def collect(self, candidate, period):
        name = candidate.repository.full_name
        documents = []
        diagnostics = {'accepted': 0, 'rejected': 0, 'reasons': {}, 'source_failures': []}
        def rejected(reason):
            diagnostics['rejected'] += 1
            diagnostics['reasons'][reason] = diagnostics['reasons'].get(reason, 0) + 1
        def append(kind, source_type, url, title, text, author=None, occurred_at=None,
                   author_association=None, github_actor_type=None):
            if text:
                limit = (rules.DEEP_COMMENT_CHAR_LIMIT if source_type.endswith('Comment') else
                         rules.DEEP_RELEASE_CHAR_LIMIT if source_type == 'Release' else
                         rules.DEEP_COMMUNITY_CHAR_LIMIT if kind in {'issues','pulls','discussions'} else
                         rules.DEEP_DOCUMENT_CHAR_LIMIT)
                if any(document['url'] == url and document['text'] == text[:limit] for document in documents):
                    rejected('duplicate')
                    return
                documents.append({'id': f'd{len(documents)+1}', 'kind': kind, 'url': url,
                    'source_type': source_type, 'title': title, 'text': text[:limit], 'author': author,
                    'author_association':author_association, 'github_actor_type':github_actor_type,
                    'occurred_at': occurred_at, 'truncated':len(text)>limit})
                diagnostics['accepted'] += 1
        def representative_comments(items):
            selected, fingerprints = [], []
            for item in items:
                if not item.get('body') or not period.contains(item['created_at']):
                    continue
                words = set(re.findall(r'[\w]+', item['body'].casefold()))
                similar = any(words and len(words & old) / max(1, len(words | old)) >= 0.72
                              for old in fingerprints)
                if similar:
                    rejected('duplicate')
                    continue
                if len(selected) >= rules.DEEP_COMMENT_PER_PARENT:
                    rejected('budget_trimmed')
                    continue
                selected.append(item)
                fingerprints.append(words)
            return selected
        def representative_events(events):
            security = sorted((event for event in events if event.source == 'security'),
                              key=lambda event:event.occurred_at, reverse=True)
            releases = sorted((event for event in events if event.source == 'release'),
                key=lambda event:(rules.RELEASE_SCORES.get(event.kind, 0), event.occurred_at), reverse=True)
            meaningful = [event for event in releases if rules.RELEASE_SCORES.get(event.kind, 0) > 0]
            patches = [event for event in releases if rules.RELEASE_SCORES.get(event.kind, 0) == 0]
            selected = security + meaningful[:rules.DEEP_LIMITS['releases']]
            remaining = max(0, rules.DEEP_LIMITS['releases'] - len(meaningful[:rules.DEEP_LIMITS['releases']]))
            if remaining and patches:
                selected.append(patches[0])
            chosen = {event.identity for event in selected}
            for event in events:
                if event.source in {'release','security'} and event.identity not in chosen:
                    rejected('budget_trimmed')
            return selected
        def attempt(label, function, endpoint=None, source_type=None, optional=False):
            try:
                return function()
            except (GitHubError, ValueError, KeyError) as error:
                candidate.errors.append(f'deep {label}: {type(error).__name__}')
                # Only allowlisted metadata: never log response bodies or arbitrary exception text.
                status = re.fullmatch(r'GitHub HTTP (\d{3})',str(error))
                category = ('API_BUDGET_EXHAUSTED' if isinstance(error,ApiBudgetExceeded) else
                            'RATE_LIMIT' if isinstance(error,RateLimitError) else
                            'HTTP_ERROR' if status else
                            'TRANSPORT_ERROR' if str(error) == 'GitHub transport failure' else
                            'INVALID_RESPONSE' if isinstance(error,(ValueError,KeyError)) else 'GITHUB_ERROR')
                http_status = (int(status[1]) if status else
                               getattr(self.github,'last_status_code',None)
                               if isinstance(error,RateLimitError) else None)
                failure = {'source':label,'source_type':source_type or label,
                           'endpoint':endpoint,'http_status':http_status,
                           'category':category,'optional':optional}
                diagnostics['source_failures'].append(failure)
                logger.warning('Deep source unavailable repository=%s diagnostic=%s',name,failure)
                return None
        readme_path = f'repos/{name}/readme'
        readme = attempt('readme', lambda: self.github.request(readme_path),readme_path,'README')
        if readme:
            content = attempt('readme decode', lambda: base64.b64decode(readme['content']).decode('utf-8'))
            append('readme', 'README', readme.get('html_url', candidate.repository.url), 'README', content)
        # Optional repository-local convention; this path is not guaranteed to exist.
        docs_path = f'repos/{name}/contents/docs/index.md'
        docs = attempt('official docs', lambda: self.github.request(docs_path),
                       docs_path,'OfficialDocs',optional=True)
        if docs and isinstance(docs, dict) and docs.get('content'):
            content = attempt('docs decode', lambda: base64.b64decode(docs['content']).decode('utf-8'))
            append('official_docs', 'OfficialDocs', docs.get('html_url', candidate.repository.url), 'Official documentation', content)
        for kind in ('issues', 'pulls'):
            items = attempt(kind, lambda: getattr(self.github, kind)(name, query_window(period), rules.DEEP_LIMITS[kind])) or []
            for item in items:
                if not period.contains(item['created_at']):
                    continue
                source_type = 'Issue' if kind == 'issues' else 'PR'
                append(kind, source_type, item['html_url'], item['title'], item.get('body'),
                       (item.get('user') or {}).get('login'), item['created_at'],
                       item.get('author_association'), (item.get('user') or {}).get('type'))
                comments = attempt(kind + ' comments', lambda: self.github.request(
                    f"repos/{name}/issues/{item['number']}/comments", {'per_page': 10})) or []
                for comment in representative_comments(comments):
                    comment_type = 'IssueComment' if kind == 'issues' else 'PRComment'
                    append('comment', comment_type, comment['html_url'], item['title'], comment.get('body'),
                           (comment.get('user') or {}).get('login'), comment['created_at'],
                           comment.get('author_association'), (comment.get('user') or {}).get('type'))
        discussions = attempt('discussions', lambda: self.github.discussions(name, rules.DEEP_LIMITS['discussions'])) or []
        for item in discussions:
            # An old discussion's newly updated body cannot prove a historical weekly event.
            if period.contains(item['createdAt']):
                append('discussions', 'Discussion', item['url'], item['title'], item['body'],
                       (item.get('author') or {}).get('login'), item['createdAt'],
                       github_actor_type=(item.get('author') or {}).get('__typename'))
            raw_comments = [{'body':comment.get('body'), 'html_url':comment.get('url'),
                'created_at':comment.get('createdAt'), 'user':comment.get('author')}
                for comment in item.get('comments', {}).get('nodes', [])]
            for comment in representative_comments(raw_comments):
                append('comment', 'DiscussionComment', comment['html_url'], item['title'], comment['body'],
                       (comment.get('user') or {}).get('login'), comment['created_at'],
                       github_actor_type=(comment.get('user') or {}).get('__typename'))
        for event in representative_events(candidate.events):
            if not period.contains(event.occurred_at):
                rejected('outside_period')
                continue
            source_type = {'release':'Release', 'security':'Security'}.get(event.source, 'Other')
            append(event.source, source_type, event.url, event.title,
                event.details.get('body') or event.details.get('description') or event.title,
                occurred_at=event.occurred_at)
        # Keep official context and event evidence before filling the comment budget.
        priority = {'readme':0,'official_docs':0,'release':1,'security':1,
                    'issues':2,'pulls':2,'discussions':2,'comment':3}
        prioritized = sorted(documents,key=lambda d:priority.get(d['kind'],9))
        bounded, used = [], 0
        for document in prioritized:
            size = len(document['text'])
            if used+size <= rules.DEEP_TOTAL_CHAR_LIMIT:
                bounded.append(document)
                used += size
            else:
                rejected('budget_trimmed')
        if len(bounded)<len(documents) or any(d['truncated'] for d in bounded):
            candidate.errors.append('deep evidence limited to configured text budget; partial document coverage')
        diagnostics['accepted'] = len(bounded)
        candidate.deep_analysis_manifest = {
            'schema_version': 2, 'repository': name,
            'period_start': period.start.isoformat(), 'period_end': period.end.isoformat(),
            'document_count': len(bounded), 'evidence_chars': used,
        }
        candidate.deep_analysis_diagnostics = diagnostics
        return bounded

    def collect_top(self, top, period):
        if len(top) > rules.TOP_COUNT:
            raise ValueError('Deep analysis restricted to final TOP 5')
        return {c.repository.identity: self.collect(c, period) for c in top}
