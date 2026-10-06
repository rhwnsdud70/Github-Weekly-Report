from copy import deepcopy
from config import rules
from models.domain import instant


def same_repository(left, right):
    a, b = left.repository, right.repository
    if a.github_id is not None and b.github_id is not None:
        return a.github_id == b.github_id
    if a.url and b.url:
        return a.url.rstrip('/').removesuffix('.git').lower() == b.url.rstrip('/').removesuffix('.git').lower()
    return a.full_name.lower() == b.full_name.lower()


def merge_candidates(candidates):
    merged = []
    for candidate in candidates:
        existing = next((item for item in merged if same_repository(item, candidate)), None)
        if existing is None:
            merged.append(deepcopy(candidate))
            continue
        existing.collection_sources |= candidate.collection_sources
        identities = {event.identity for event in existing.events}
        existing.events.extend(deepcopy(event) for event in candidate.events if event.identity not in identities)
        for key, metric in candidate.metrics.items():
            if existing.metric(key).value is None:
                existing.metrics[key] = metric
            elif metric.value is not None and metric.value != existing.metric(key).value:
                existing.errors.append(f'conflicting metric: {key}; first observation retained')
        if candidate.score and (not existing.score or candidate.score.total > existing.score.total):
            existing.score = deepcopy(candidate.score)
        if existing.repository.github_id is None and candidate.repository.github_id is not None:
            existing.repository = deepcopy(candidate.repository)
        existing.signals |= candidate.signals
        existing.errors.extend(candidate.errors)
        existing.changes_since_report = list(dict.fromkeys(existing.changes_since_report + candidate.changes_since_report))
    return merged


def allow_repeat(candidate, period):
    reports = candidate.previous.get('reports', [])
    if not reports:
        return True
    last = reports[0]
    seen = {tuple(event) for report in reports for event in report['data'].get('event_ids', [])}
    changes = [f'{event.title} ({event.url})' for event in candidate.events
               if event.kind in rules.REPEAT_EVENTS and event.identity not in seen
               and instant(event.occurred_at) >= instant(last['period_end'])
               and period.contains(event.occurred_at)]
    if 'star' in candidate.signals and instant(last['period_end']) <= period.start:
        changes.append(f"지난 소개 이후 이번 주 Star 증가: {candidate.metric('star_delta').value}; 증가율: {candidate.metric('star_growth').value}%")
    candidate.changes_since_report = changes
    if not changes and not candidate.critical:
        candidate.validation_reasons.append('previously reported without meaningful new change')
        return False
    return True
