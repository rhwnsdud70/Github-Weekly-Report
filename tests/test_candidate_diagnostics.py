from datetime import datetime, timedelta, timezone

from config import rules
from models.domain import Candidate, Metric, Repository, ScoreResult
from services.candidate_diagnostics import build_candidate_diagnostics, format_candidate_diagnostics
from services.ranking_service import select_top


def candidate(index, *, reasons=(), signals=('star', 'activity'), unavailable=False, score=50):
    metrics = {
        'star_delta': Metric.known(100),
        'star_growth_rate': Metric.known(10),
        'issues': Metric(None, 'unavailable') if unavailable else Metric.known(12),
        'pulls': Metric.known(7),
        'trending': Metric(),
        'releases_checked': Metric.known(True),
        'security_checked': Metric.known(True),
    }
    result = Candidate(
        Repository(index, f'owner/repo{index}', f'https://github.com/owner/repo{index}', stars=1000),
        collection_sources={'search', 'issue'}, metrics=metrics,
        score=ScoreResult({'score': score}), signals=set(signals),
        validation_reasons=list(reasons))
    return result


def test_candidate_funnel_and_exclusion_reason_aggregation():
    candidates = [
        candidate(1),
        candidate(2, reasons=('AI/developer relevance not established',), signals=()),
        candidate(3, reasons=('insufficient independent GitHub evidence',), signals=('star',)),
        candidate(4, reasons=('previously reported without meaningful new change',), unavailable=True),
    ]
    candidates[1].repository.relevant = False
    diagnostic = build_candidate_diagnostics(
        candidates, {'discovered': 6, 'duplicates': 2}, candidates,
        historical_trending_unavailable=True)

    assert diagnostic['funnel'] == {
        'discovered': 6, 'deduplicated': 4, 'metrics attempted': 4,
        'metrics completed': 3, 'relevance eligible': 3,
        'evidence supported': 2, 'scoring eligible': 2, 'selectable': 1,
    }
    assert diagnostic['exclusion_reasons'] == {
        'ai_developer_relevance_not_established': 1,
        'duplicate_repository': 2,
        'insufficient_independent_evidence': 1,
        'metric_unavailable': 1,
        'previously_reported_without_meaningful_change': 1,
    }
    assert diagnostic['kind'] == 'METRIC_COLLECTION_DEGRADED'


def test_historical_trending_is_context_not_failure_reason():
    diagnostic = build_candidate_diagnostics(
        [candidate(1)],
        historical_trending_unavailable=True)
    rendered = format_candidate_diagnostics(diagnostic)
    assert 'Historical Trending: unavailable (expected for retrospective manual run)' in rendered
    assert 'historical_trending' not in diagnostic['exclusion_reasons']


def test_less_than_five_includes_at_most_ten_safe_candidate_details():
    candidates = [candidate(index, reasons=('insufficient independent GitHub evidence',),
                            score=100-index) for index in range(12)]
    diagnostic = build_candidate_diagnostics(candidates)
    assert len(diagnostic['top_candidates']) == 10
    assert set(diagnostic['top_candidates'][0]) == {
        'repository', 'total_score', 'grade', 'evidence_source_count',
        'metrics_available', 'metrics_unavailable', 'selection_status', 'exclusion_reasons'}


def test_diagnostics_do_not_change_existing_top_five_or_add_score_threshold():
    candidates = [candidate(index, score=index) for index in range(1, 7)]
    diagnostic = build_candidate_diagnostics(candidates, metric_candidates=candidates)
    assert diagnostic['funnel']['selectable'] == 6
    assert diagnostic['top_candidates'] == []
    assert 'score_below_threshold' not in diagnostic['exclusion_reasons']

    end = datetime(2026, 1, 11, tzinfo=timezone.utc)
    top, _ = select_top(candidates, type('Period', (), {
        'start': end-timedelta(days=7), 'end': end,
        'contains': lambda self, value: True})())
    assert len(top) == rules.TOP_COUNT
    assert {item.repository.full_name for item in top} == {
        'owner/repo2', 'owner/repo3', 'owner/repo4', 'owner/repo5', 'owner/repo6'}
