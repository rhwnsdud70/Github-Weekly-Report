"""Explicit cold-start fixtures for the isolated manual integration runner."""
from dataclasses import asdict

from config import rules
from models.domain import Metric
from services.metric_service import calculate_metrics
from services.ranking_service import InsufficientCandidates


class ColdStartIntegrationFixture:
    disclosure = ('INTEGRATION TEST FIXTURE: previous star/fork/activity snapshots and historical '
                  'Trending signals are synthetic; current repository and event data come from GitHub.')

    def __init__(self):
        self.identities = []

    def observed_at(self, period):
        return period.end.isoformat()

    def seed_history(self, database, candidates, period):
        """Seed only shortlist-eligible, relevant repositories in the isolated test DB."""
        eligible = [candidate for candidate in candidates[:rules.CANDIDATE_METRIC_LIMIT]
                    if candidate.repository.relevant and not candidate.repository.excluded_reason
                    and candidate.repository.stars is not None]
        if len(eligible) < rules.TOP_COUNT:
            raise InsufficientCandidates(
                f'Integration fixture found only {len(eligible)} relevant metric candidates')
        self.identities = [candidate.repository.identity for candidate in eligible]
        with database.transaction():
            for index,candidate in enumerate(eligible,1):
                repo = candidate.repository
                absolute,growth = rules.star_threshold(repo.stars)
                target = absolute or max(1,round(repo.stars*(growth or 10)/100))
                delta = min(max(1,target*2),max(1,repo.stars-1))
                previous_stars = max(1,repo.stars-delta)
                previous_forks = (max(0,repo.forks-max(1,min(repo.forks,5)))
                                  if repo.forks is not None else None)
                database.repository(repo.identity,asdict(repo))
                database.snapshot(repo.identity,period.start.isoformat(),{
                    'measured_at':period.start.isoformat(),
                    'stars':previous_stars,'forks':previous_forks,
                    'issues':max(0,index-1),'pulls':max(0,index//2-1),
                    'discussion_total':None,
                    'fixture':'integration-test previous snapshot'})

    def apply_metrics(self, candidates, period):
        """Add unavailable historical signals, then use the production metric calculator."""
        seeded = set(self.identities)
        for rank,candidate in enumerate((c for c in candidates if c.repository.identity in seeded),1):
            snapshot = candidate.previous.get('snapshot') or {}
            candidate.metrics['previous_issues'] = Metric.known(
                snapshot.get('issues',0),'INTEGRATION TEST FIXTURE previous activity')
            candidate.metrics['previous_pulls'] = Metric.known(
                snapshot.get('pulls',0),'INTEGRATION TEST FIXTURE previous activity')
            candidate.metrics['trending'] = Metric.known(
                True,'INTEGRATION TEST FIXTURE historical Trending')
            candidate.metrics['trending_rank'] = Metric.known(
                rank,'INTEGRATION TEST FIXTURE historical Trending rank')
            candidate.collection_sources.add('integration-test-fixture')
            calculate_metrics(candidate,period)
            for name in ('star_delta','star_growth','fork_delta','fork_growth',
                         'issues_growth','pulls_growth','activity_growth'):
                metric = candidate.metric(name)
                candidate.metrics[name] = Metric(metric.value,metric.status,
                    'INTEGRATION TEST FIXTURE comparison; current GitHub values preserved')

    def summary(self):
        return {
            'fixture_only_fields':['previous_snapshot','previous_activity','historical_trending'],
            'seeded_candidate_count':len(self.identities),
            'disclosure':self.disclosure,
        }
