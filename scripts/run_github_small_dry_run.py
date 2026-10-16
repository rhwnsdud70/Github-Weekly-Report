"""Collect and rank up to ten live GitHub candidates without OpenAI or Telegram.

Run from the repository root:
    python scripts/run_github_small_dry_run.py
"""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from collectors.github_client import GitHubClient
from config.rules import API_BUDGETS, CANDIDATE_LIMIT, CANDIDATE_METRIC_LIMIT
from config.settings import Settings
from database.db import Database
from models.domain import Metric
from services.candidate_service import CandidateCollector
from services.duplicate_service import merge_candidates, allow_repeat
from services.history_service import attach_history
from services.metric_service import collect_metrics_batch
from services.ranking_service import InsufficientCandidates, select_top
from services.scheduler import report_period
from services.score_service import score_candidate
from services.validation_service import validate_candidate


LIMIT = min(10, CANDIDATE_LIMIT)


def metric_value(candidate, name, suffix=''):
    metric = candidate.metric(name)
    if metric.status != 'confirmed':
        return f'unknown ({metric.status})'
    value = metric.value
    if isinstance(value, float):
        return f'{value:.2f}{suffix}'
    return f'{value}{suffix}'


def events(candidate, source):
    names = [f'{event.kind}: {event.title}' for event in candidate.events if event.source == source]
    return '; '.join(names) if names else 'none'


def trending(candidate):
    present = candidate.metric('trending')
    rank = candidate.metric('trending_rank')
    if present.status != 'confirmed':
        return f'unknown ({present.status})'
    if not present.value:
        return 'not listed'
    return f'listed, rank {rank.value}' if rank.status == 'confirmed' else 'listed, rank unknown'


def initial_reason(candidate):
    if candidate.validation_reasons:
        return '; '.join(candidate.validation_reasons)
    if candidate.critical:
        return 'Critical Security candidate: mandatory final TOP 5 review'
    return 'eligible for score-based ranking'


def candidate_summary(candidate, selected, selected_reasons):
    score = candidate.score
    reason = selected_reasons.get(candidate.repository.identity)
    if candidate.repository.identity not in selected and not candidate.validation_reasons:
        reason = 'eligible but ranked below final TOP 5'
    return {
        'repository': candidate.repository.full_name,
        'collection_source': ', '.join(sorted(candidate.collection_sources)) or 'unknown',
        'current_stars': metric_value(candidate, 'stars') if 'stars' in candidate.metrics else (
            str(candidate.repository.stars) if candidate.repository.stars is not None else 'unknown'),
        '7_day_star_delta': metric_value(candidate, 'star_delta'),
        'trending': trending(candidate),
        'issue_pr_activity': f"issues={metric_value(candidate, 'issues')}, prs={metric_value(candidate, 'pulls')}",
        'release_event': events(candidate, 'release'),
        'security_event': events(candidate, 'security'),
        'total_score': score.total if score else 'unknown',
        'grade': score.grade if score else 'unknown',
        'top_5': candidate.repository.identity in selected,
        'reason': reason or initial_reason(candidate),
    }


def run(settings, github, web_client, limit=LIMIT):
    """Return an audit object. The in-memory DB guarantees no saved history."""
    if not settings.github_token:
        raise ValueError('GITHUB_TOKEN is missing or empty')
    period = report_period()
    database = Database(':memory:')
    collector = CandidateCollector(github, web_client)
    try:
        github.begin_budget('candidate', API_BUDGETS['candidate'])
        try:
            candidates = merge_candidates(collector.collect(period, limit=limit))
        finally:
            github.end_budget()
        github.begin_budget('validation', API_BUDGETS['validation'])
        try:
            for candidate in candidates:
                attach_history(candidate, database, period)
            metric_candidates = [candidate for index, candidate in enumerate(candidates)
                                 if index < CANDIDATE_METRIC_LIMIT or candidate.critical]
            collect_metrics_batch(metric_candidates, github, period)
            for candidate in candidates:
                if candidate not in metric_candidates:
                    for name in ('issues', 'pulls', 'previous_issues', 'previous_pulls', 'releases_checked'):
                        candidate.metrics[name] = Metric(source='candidate metric shortlist budget')
                validate_candidate(candidate, period)
                if not candidate.validation_reasons:
                    allow_repeat(candidate, period)
                score_candidate(candidate)
        finally:
            github.end_budget()

        top, watchlist = [], []
        selection_error = None
        try:
            top, watchlist = select_top(candidates, period)
        except InsufficientCandidates as error:
            selection_error = str(error)
        selected = {candidate.repository.identity for candidate in top}
        selected_reasons = {candidate.repository.identity: '; '.join(candidate.selection_reasons)
                            for candidate in top}
        errors = [*collector.errors, *[
            f'{candidate.repository.full_name}: {error}'
            for candidate in candidates for error in candidate.errors
        ]]
        return {
            'period_start': period.start.isoformat(),
            'period_end': period.end.isoformat(),
            'candidate_limit': limit,
            'candidate_count': len(candidates),
            'candidates': [candidate_summary(candidate, selected, selected_reasons)
                           for candidate in candidates],
            'top_5': [candidate.repository.full_name for candidate in top],
            'watchlist': [candidate.repository.full_name for candidate in watchlist],
            'selection_error': selection_error,
            'api_errors': errors,
            'api_usage': github.request_summary(),
        }
    finally:
        database.close()


def print_audit(audit):
    print(f"Candidate count: {audit['candidate_count']} / {audit['candidate_limit']}")
    print(f"Period: {audit['period_start']} <= event < {audit['period_end']}")
    for item in audit['candidates']:
        print()
        for key in ('repository', 'collection_source', 'current_stars', '7_day_star_delta',
                    'trending', 'issue_pr_activity', 'release_event', 'security_event',
                    'total_score', 'grade', 'top_5', 'reason'):
            print(f'{key}: {item[key]}')
    print()
    print('Final TOP 5: ' + (', '.join(audit['top_5']) if audit['top_5'] else 'not selected'))
    if audit['selection_error']:
        print('Selection status: ' + audit['selection_error'])
    print('API errors: ' + ('; '.join(audit['api_errors']) if audit['api_errors'] else 'none'))
    usage = audit['api_usage']
    print(f"API requests: REST={usage['total_rest_requests']}, GraphQL={usage['total_graphql_requests']}")
    print('Endpoint requests: ' + (', '.join(f'{key}={value}' for key, value in sorted(usage['endpoint_requests'].items())) or 'none'))
    print('Rate limits: ' + (str(usage['rate_limits']) if usage['rate_limits'] else 'not provided by GitHub'))
    unknown = sum(item['7_day_star_delta'].startswith('unknown') for item in audit['candidates'])
    print(f'Unknown 7-day star delta: {unknown}')


def main():
    settings = Settings.load('.env')
    with httpx.Client(timeout=30) as web_client:
        github = GitHubClient(settings.github_token)
        try:
            audit = run(settings, github, web_client)
        finally:
            github.close()
    print_audit(audit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
