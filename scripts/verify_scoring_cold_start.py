"""Fixture-only Cold Start scoring/ranking verification; never calls external APIs."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import Database
from models.domain import Candidate, Event, Metric, ReportPeriod, Repository
from services.duplicate_service import merge_candidates, allow_repeat
from services.history_service import attach_history
from services.metric_service import calculate_metrics
from services.ranking_service import select_top
from services.score_service import score_candidate
from services.validation_service import validate_candidate


def period():
    end = datetime(2026, 10, 4, 19, tzinfo=timezone(timedelta(hours=9)))
    return ReportPeriod(end - timedelta(days=7), end)


def candidates(database, report_period):
    result = []
    for number in range(1, 11):
        previous_stars = 1_000 + number * 100
        current_stars = previous_stars + (10 if number == 10 else 200 + number * 15)
        repo = Repository(number, f'fixture/ai-project-{number}',
            f'https://github.com/fixture/ai-project-{number}', category='AI',
            stars=current_stars, forks=100 + number,
            created_at=(report_period.start-timedelta(days=90)).isoformat())
        database.repository(repo.identity, asdict(repo))
        database.snapshot(repo.identity, report_period.start.isoformat(), {
            'measured_at': report_period.start.isoformat(), 'stars': previous_stars,
            'forks': 90 + number, 'issues': 5, 'pulls': 3, 'discussion_total': 2})
        candidate = Candidate(repo, {'search', 'trending'})
        candidate.metrics.update({
            'issues': Metric.known(12 + number), 'pulls': Metric.known(7 + number),
            'previous_issues': Metric.known(5), 'previous_pulls': Metric.known(3),
            'trending': Metric.known(True), 'trending_rank': Metric.known(number),
            'releases_checked': Metric.known(True), 'security_checked': Metric.known(True),
        })
        if number == 1:
            candidate.events.append(Event('major', 'Major release', 'https://example/r1',
                (report_period.end-timedelta(days=2)).isoformat(), 'release'))
        if number == 2:
            candidate.events.append(Event('critical', 'Critical security fix', 'https://example/r2',
                (report_period.end-timedelta(days=1)).isoformat(), 'security'))
        if number == 10:
            database.history(repo.identity, report_period.start.isoformat(), 1, 80,
                {'event_ids': [], 'selection_reasons': []})
        attach_history(candidate, database, report_period, report_period.end)
        calculate_metrics(candidate, report_period)
        validate_candidate(candidate, report_period)
        if not candidate.validation_reasons:
            allow_repeat(candidate, report_period)
        score_candidate(candidate)
        result.append(candidate)
    # A second discovery event for repo 1 must merge into its one candidate.
    duplicate = Candidate(result[0].repository, {'security'}, [Event('high', 'Security advisory',
        'https://example/r1-security', (report_period.end-timedelta(days=1)).isoformat(), 'security')])
    return merge_candidates(result + [duplicate])


def main():
    report_period = period()
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / 'cold-start.db')
        try:
            items = candidates(database, report_period)
            top, _ = select_top(items, report_period)
            ranks = {candidate.repository.identity: rank for rank, candidate in enumerate(top, 1)}
            for candidate in items:
                previous = candidate.previous['snapshot']['stars']
                score = candidate.score
                print(candidate.repository.full_name)
                print(f'  previous stars: {previous}; current stars: {candidate.repository.stars}')
                print(f"  star delta: {candidate.metric('star_delta').value}; star growth %: {candidate.metric('star_growth').value:.2f}")
                print(f'  component scores: {score.components}')
                print(f'  total score: {score.total}; grade: {score.grade}; final rank: {ranks.get(candidate.repository.identity, "excluded")}')
            print('Final TOP 5: ' + ', '.join(candidate.repository.full_name for candidate in top))
            assert len(top) == 5 and len({candidate.repository.identity for candidate in top}) == 5
            assert any(candidate.critical for candidate in top)
            assert len(items) == 10 and all(candidate.repository.category == 'AI' for candidate in top)
            assert next(candidate for candidate in items if candidate.repository.github_id == 10).validation_reasons
            return 0
        finally:
            database.close()


if __name__ == '__main__':
    raise SystemExit(main())
