from config import rules
from services.duplicate_service import merge_candidates, allow_repeat
from services.validation_service import validate_candidate
from services.score_service import score_candidate


class InsufficientCandidates(RuntimeError):
    pass


def ranking_key(candidate):
    official_events = sum(event.source in {'release', 'security'} for event in candidate.events)
    known = sum(metric.status == 'confirmed' for metric in candidate.metrics.values())
    return (candidate.score.total, official_events, len(candidate.signals), known)


def rank_candidates(candidates, period):
    # Preserve an already calculated highest representative event score when merging.
    merged = merge_candidates(candidates)
    eligible = []
    for candidate in merged:
        if not validate_candidate(candidate, period) or not allow_repeat(candidate, period):
            continue
        if candidate.score is None:
            score_candidate(candidate)
        candidate.selection_reasons = ['AI/developer relevance confirmed',
            'independent signals: ' + ', '.join(sorted(candidate.signals)),
            f'confirmed metrics: {sum(m.status == "confirmed" for m in candidate.metrics.values())}',
            'official impact events: ' + ', '.join(e.kind for e in candidate.events if e.source in {'release','security'})]
        eligible.append(candidate)
    return sorted(eligible, key=ranking_key, reverse=True)


def select_top(candidates, period):
    ranked = rank_candidates(candidates, period)
    if len(ranked) < rules.TOP_COUNT:
        raise InsufficientCandidates(f'Only {len(ranked)} evidence-supported repositories; TOP 5 not fabricated')
    critical = [candidate for candidate in ranked if candidate.critical]
    if len(critical) > rules.TOP_COUNT:
        raise InsufficientCandidates('More than five distinct Critical repositories: TOP 5 and mandatory inclusion conflict')
    top = ranked[:rules.TOP_COUNT]
    for candidate in critical:
        if candidate not in top:
            replace = next(i for i in range(len(top)-1, -1, -1) if not top[i].critical)
            top[replace] = candidate
            candidate.selection_reasons.append('Critical Security mandatory inclusion')
    top.sort(key=lambda candidate: (candidate.score.total + security_priority(candidate), ranking_key(candidate)), reverse=True)
    selected = {candidate.repository.identity for candidate in top}
    watch = [candidate for candidate in ranked if candidate.repository.identity not in selected][:rules.WATCHLIST_LIMIT]
    return top, watch


def security_priority(candidate):
    if not candidate.critical:
        return 0
    # Only explicitly evidenced factors; absent data adds no urgency points.
    factors = {key for event in candidate.events if event.source == 'security'
               for key in rules.SECURITY_PRIORITY_WEIGHTS if event.details.get(key) is True}
    scale = next((bonus for minimum,bonus in rules.SECURITY_PROJECT_SCALE
                  if (candidate.repository.stars or 0) >= minimum),0)
    return sum(rules.SECURITY_PRIORITY_WEIGHTS[key] for key in factors) + scale
