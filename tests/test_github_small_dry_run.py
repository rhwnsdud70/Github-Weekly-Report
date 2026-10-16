from datetime import datetime, timedelta, timezone

from config.settings import Settings
from models.domain import Event, Repository
from scripts.run_github_small_dry_run import candidate_summary
from services.score_service import score_candidate
from models.domain import Candidate, Metric


def test_candidate_summary_preserves_unknown_and_exclusion_reason():
    candidate = Candidate(Repository(1, 'owner/project', 'https://github.com/owner/project', stars=100))
    candidate.metrics = {
        'star_delta': Metric(source='no prior snapshot'),
        'issues': Metric.known(0),
        'pulls': Metric.known(0),
    }
    candidate.validation_reasons = ['insufficient independent GitHub evidence']
    score_candidate(candidate)
    summary = candidate_summary(candidate, set(), {})
    assert summary['7_day_star_delta'] == 'unknown (unknown)'
    assert summary['issue_pr_activity'] == 'issues=0, prs=0'
    assert summary['top_5'] is False
    assert 'insufficient' in summary['reason']
