import logging
from datetime import timedelta
from config import rules
from collectors.github_client import GitHubError
from collectors.github_repository import repository_model
from collectors.github_security import advisory_repository, security_event
from collectors.github_trending import collect_trending
from collectors.github_issue import issue_event
from models.domain import Candidate, Event, Metric

logger = logging.getLogger(__name__)


class CandidateCollector:
    def __init__(self, github, web_client, trending=collect_trending):
        self.github, self.web_client, self.trending = github, web_client, trending
        self.errors = []

    def collect(self, period, limit=rules.CANDIDATE_LIMIT, include_trending=True):
        candidates = {}
        def add(name, source, rank=None, event=None, raw=None):
            try:
                repo = repository_model(raw or self.github.repository(name))
            except GitHubError as error:
                self.errors.append(f'repository {name}: {error}')
                return
            candidate = candidates.setdefault(repo.identity, Candidate(repo))
            candidate.collection_sources.add(source)
            if rank is not None:
                candidate.metrics['trending'] = Metric.known(True, 'https://github.com/trending?since=weekly')
                candidate.metrics['trending_rank'] = Metric.known(rank, 'observed weekly page position')
            if event:
                candidate.events.append(event)
                if event.source == 'security':
                    candidate.repository.relevant = True
        try:
            if include_trending:
                for name, rank in self.trending(self.web_client)[:round(limit * .4)]:
                    add(name, 'trending', rank)
        except GitHubError as error:
            self.errors.append(str(error))
        date = (period.end - timedelta(days=14)).date().isoformat()
        for topic in rules.SEARCH_TOPICS:
            try:
                result = self.github.search('repositories', f'topic:{topic} stars:>=100 pushed:>={date} fork:false',
                                            max(2, round(limit * .4 / len(rules.SEARCH_TOPICS))))
                for raw in result['items']:
                    add(raw['full_name'], 'search', raw=raw)
            except GitHubError as error:
                self.errors.append(f'search {topic}: {error}')
        query = f'is:issue created:{period.start.isoformat()}..{(period.end-timedelta(seconds=1)).isoformat()}'
        for term in ('"breaking change"', '"license change"', '"deprecation"', '"project shutdown"'):
            try:
                for item in self.github.search('issues', f'{query} {term}', max(2, limit // 10), 'comments')['items']:
                    if period.contains(item['created_at']):
                        name = item['repository_url'].split('/repos/')[-1]
                        event = issue_event(item)
                        add(name, 'issue', event=event)
            except GitHubError as error:
                self.errors.append(f'issue discovery: {error}')
        security_checked = False
        try:
            for advisory in self.github.advisories(period.start.isoformat(), period.end.isoformat()):
                event = security_event(advisory)
                name = advisory_repository(advisory)
                if event and name and period.contains(event.occurred_at):
                    add(name, 'security', event=event)
            security_checked = True
        except GitHubError as error:
            self.errors.append(f'security: {error}')
        # Security candidates are never dropped by discovery budget.
        result = list(candidates.values())
        for candidate in result:
            candidate.metrics['security_checked'] = Metric.known(True, 'global GitHub advisories') if security_checked else Metric(None,'unavailable','security collection failed')
        critical = [c for c in result if c.critical]
        ordinary = [c for c in result if not c.critical]
        for error in self.errors:
            logger.warning('%s', error)
        return critical + ordinary[:max(0, limit - len(critical))]
