from config import rules
from models.domain import instant
from services.metric_service import derive_signals


def validate_candidate(candidate, period=None):
    reasons = []
    if period:
        candidate.events = [event for event in candidate.events if period.contains(event.occurred_at)]
    derive_signals(candidate)
    if candidate.repository.excluded_reason and not candidate.critical:
        reasons.append(candidate.repository.excluded_reason)
    if not candidate.repository.relevant:
        reasons.append('AI/developer relevance not established')
    if len(candidate.signals) < rules.MIN_SIGNALS and not candidate.critical:
        reasons.append('insufficient independent GitHub evidence')
    if not candidate.repository.url:
        reasons.append('canonical repository URL missing')
    if candidate.repository.created_at and period and instant(candidate.repository.created_at) >= period.end:
        reasons.append('repository created outside report period')
    candidate.validation_reasons = reasons
    return not reasons
