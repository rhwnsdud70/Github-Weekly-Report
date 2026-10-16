"""Safe, aggregate diagnostics for an insufficient candidate funnel."""
from collections import Counter

from config import rules


MAJOR_METRICS = (
    'star_delta', 'star_growth', 'issues', 'pulls', 'trending',
    'releases_checked', 'security_checked',
)

FUNNEL_FIELDS = ('discovered', 'deduplicated', 'metrics attempted', 'metrics completed',
                 'relevance eligible', 'evidence supported', 'scoring eligible', 'selectable')
EXCLUSION_REASONS = frozenset({
    'ai_developer_relevance_not_established', 'insufficient_independent_evidence',
    'score_below_threshold', 'duplicate_repository',
    'previously_reported_without_meaningful_change', 'metric_unavailable',
    'security_evidence_insufficient', 'validation_failed', 'other'})
GAP_FIELDS = ('previous_snapshot_missing', 'previous_snapshot_unusable',
              'star_delta_unknown', 'star_growth_unknown', 'activity_unavailable')
DIAGNOSTIC_KINDS = frozenset({'NO_DISCOVERY_CANDIDATES', 'METRIC_COLLECTION_DEGRADED',
                            'INSUFFICIENT_EVIDENCE', 'INSUFFICIENT_ELIGIBLE_CANDIDATES'})


def candidate_diagnostic_summary(diagnostic):
    """Allowlisted aggregates only, safe for production console and run persistence.

    Do not echo arbitrary exception payloads, source error messages or evidence.
    Old exceptions without diagnostics remain supported without guessing counts.
    """
    if (not isinstance(diagnostic,dict) or not isinstance(diagnostic.get('kind'),str)
            or diagnostic['kind'] not in DIAGNOSTIC_KINDS):
        return None
    funnel = diagnostic.get('funnel')
    if not isinstance(funnel,dict) or any(type(funnel.get(key)) is not int or funnel[key] < 0
                                         for key in FUNNEL_FIELDS):
        return None
    def counts(value, allowed):
        if not isinstance(value,dict):
            return {}
        return {key:value[key] for key in sorted(allowed)
                if type(value.get(key)) is int and value[key] >= 0}
    return {'kind':diagnostic['kind'], 'funnel':{key:funnel[key] for key in FUNNEL_FIELDS},
            'exclusion_reasons':counts(diagnostic.get('exclusion_reasons'),EXCLUSION_REASONS),
            'data_gaps':counts(diagnostic.get('data_gaps'),GAP_FIELDS),
            'historical_trending_unavailable':diagnostic.get('historical_trending_unavailable') is True,
            'top_candidates':[]}


def canonical_reason(reason):
    value = (reason or '').strip().lower()
    if 'historical trending' in value:
        return None
    if 'ai/developer relevance' in value:
        return 'ai_developer_relevance_not_established'
    if 'insufficient independent' in value:
        return 'insufficient_independent_evidence'
    if 'score' in value and ('threshold' in value or 'below' in value):
        return 'score_below_threshold'
    if 'duplicate' in value:
        return 'duplicate_repository'
    if 'previously reported without meaningful' in value:
        return 'previously_reported_without_meaningful_change'
    if 'security' in value and ('insufficient' in value or 'evidence' in value):
        return 'security_evidence_insufficient'
    if value:
        return 'validation_failed'
    return 'other'


def _metric_summary(candidate):
    available, unavailable = [], []
    for name in MAJOR_METRICS:
        status = candidate.metric(name).status
        (available if status == 'confirmed' else unavailable).append(f'{name}:{status}')
    return available, unavailable


def _is_evidence_supported(candidate):
    return len(candidate.signals) >= rules.MIN_SIGNALS or candidate.critical


def _diagnostic_kind(funnel):
    if funnel['discovered'] == 0:
        return 'NO_DISCOVERY_CANDIDATES'
    if funnel['metrics completed'] < funnel['metrics attempted']:
        return 'METRIC_COLLECTION_DEGRADED'
    if funnel['evidence supported'] < rules.TOP_COUNT:
        return 'INSUFFICIENT_EVIDENCE'
    return 'INSUFFICIENT_ELIGIBLE_CANDIDATES'


def build_candidate_diagnostics(candidates, collector_counts=None, metric_candidates=(),
                                historical_trending_unavailable=False):
    """Describe the existing selection state without changing candidate eligibility."""
    collector_counts = collector_counts or {}
    attempted_ids = {candidate.repository.identity for candidate in metric_candidates}
    completed_ids = set()
    for candidate in metric_candidates:
        statuses = [candidate.metric(name).status for name in ('issues', 'pulls', 'releases_checked')]
        if statuses and all(status not in {'unknown', 'unavailable'} for status in statuses):
            completed_ids.add(candidate.repository.identity)

    selectable = [candidate for candidate in candidates if not candidate.validation_reasons]
    relevance = [candidate for candidate in candidates
                 if candidate.repository.relevant and
                 (not candidate.repository.excluded_reason or candidate.critical)]
    evidence = [candidate for candidate in candidates if _is_evidence_supported(candidate)]
    scoring = [candidate for candidate in candidates
               if candidate.score is not None and
               not any(canonical_reason(reason) in {
                   'ai_developer_relevance_not_established',
                   'insufficient_independent_evidence', 'validation_failed',
                   'security_evidence_insufficient'}
                   for reason in candidate.validation_reasons)]
    funnel = {
        'discovered': collector_counts.get('discovered', len(candidates)),
        'deduplicated': len(candidates),
        'metrics attempted': len(attempted_ids),
        'metrics completed': len(completed_ids),
        'relevance eligible': len(relevance),
        'evidence supported': len(evidence),
        'scoring eligible': len(scoring),
        'selectable': len(selectable),
    }

    reasons = Counter()
    duplicate_count = collector_counts.get('duplicates', 0)
    if duplicate_count:
        reasons['duplicate_repository'] += duplicate_count
    for candidate in candidates:
        mapped = {canonical_reason(reason) for reason in candidate.validation_reasons}
        reasons.update(reason for reason in mapped if reason)
        if candidate.repository.identity in attempted_ids and any(
                candidate.metric(name).status == 'unavailable'
                for name in ('issues', 'pulls', 'releases_checked')):
            reasons['metric_unavailable'] += 1

    details = []
    gaps = Counter()
    for candidate in candidates:
        snapshot = candidate.previous.get('snapshot')
        if not snapshot:
            gaps['previous_snapshot_missing'] += 1
        elif candidate.previous.get('snapshot_usable') is False:
            gaps['previous_snapshot_unusable'] += 1
        for name in ('star_delta', 'star_growth'):
            if candidate.metric(name).status == 'unknown':
                gaps[name+'_unknown'] += 1
        if candidate.repository.identity in attempted_ids and any(
                candidate.metric(name).status == 'unavailable' for name in ('issues','pulls')):
            gaps['activity_unavailable'] += 1
    if len(selectable) < rules.TOP_COUNT:
        ordered = sorted(candidates, key=lambda item: item.score.total if item.score else -1,
                         reverse=True)[:10]
        for candidate in ordered:
            available, unavailable = _metric_summary(candidate)
            item_reasons = sorted({canonical_reason(reason)
                                   for reason in candidate.validation_reasons
                                   if canonical_reason(reason)})
            details.append({
                'repository': candidate.repository.full_name,
                'total_score': candidate.score.total if candidate.score else None,
                'grade': candidate.score.grade if candidate.score else None,
                'evidence_source_count': len(candidate.signals),
                'metrics_available': available,
                'metrics_unavailable': unavailable,
                'selection_status': 'selectable' if not candidate.validation_reasons else 'excluded',
                'exclusion_reasons': item_reasons,
            })
    return {
        'kind': _diagnostic_kind(funnel),
        'funnel': funnel,
        'exclusion_reasons': dict(sorted(reasons.items())),
        'data_gaps': dict(sorted(gaps.items())),
        'historical_trending_unavailable': bool(historical_trending_unavailable),
        'top_candidates': details,
    }


def format_candidate_diagnostics(diagnostic):
    lines = ['Candidate funnel:']
    for name, count in diagnostic['funnel'].items():
        lines.append(f'- {name}: {count}')
    if diagnostic['historical_trending_unavailable']:
        lines.append('Historical Trending: unavailable (expected for retrospective manual run)')
    lines.append(f'InsufficientCandidates category: {diagnostic["kind"]}')
    lines.append('Exclusion reasons:')
    if diagnostic['exclusion_reasons']:
        lines.extend(f'- {name}: {count}' for name, count in diagnostic['exclusion_reasons'].items())
    else:
        lines.append('- none: 0')
    if diagnostic.get('data_gaps'):
        lines.append('Data gaps (counts may overlap):')
        lines.extend(f'- {name}: {count}' for name, count in diagnostic['data_gaps'].items())
    if diagnostic.get('top_candidates'):
        lines.append('Top candidate diagnostics:')
        for item in diagnostic['top_candidates']:
            reasons = ','.join(item['exclusion_reasons']) or 'none'
            available = ','.join(item['metrics_available']) or 'none'
            unavailable = ','.join(item['metrics_unavailable']) or 'none'
            lines.append(
                f'- {item["repository"]} | total_score={item["total_score"]} | '
                f'grade={item["grade"]} | evidence_sources={item["evidence_source_count"]} | '
                f'metrics_available={available} | metrics_unavailable={unavailable} | '
                f'status={item["selection_status"]} | reasons={reasons}')
    return '\n'.join(lines)
