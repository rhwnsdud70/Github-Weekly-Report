import json
from models.domain import Metric
from services.observation_timing import observation_allowed


def attach_history(candidate, database, period, observed_at=None):
    identity = candidate.repository.identity
    snapshot = database.previous_snapshot(identity, period.start.isoformat())
    reports = database.previous_reports(identity, period.end.isoformat())
    score = database.rows('SELECT score FROM weekly_metrics WHERE identity=? AND period_end=?',
                          (identity, period.start.isoformat()))
    candidate.previous = {'snapshot': snapshot, 'reports': reports,
        'score': json.loads(score[0]['score']) if score else None,
        'watchlist': database.was_watched(identity, period.start.isoformat())}
    usable = True
    reasons = []
    if not snapshot:
        reasons.append('previous_snapshot_missing')
    if observed_at is not None:
        if not observation_allowed(observed_at,period.end):
            reasons.append('current_observation_outside_window')
        if snapshot and not observation_allowed(snapshot['measured_at'],period.start):
            reasons.append('previous_observation_outside_window')
        usable = not reasons
    candidate.previous['snapshot_usable'] = usable
    candidate.previous['snapshot_unusable_reasons'] = reasons
    current = candidate.repository.stars
    previous = snapshot.get('stars') if snapshot and usable else None
    if current is not None and previous is not None:
        candidate.metrics['star_delta'] = Metric.known(current - previous, 'weekly snapshots')
        candidate.metrics['star_growth'] = (Metric.known((current - previous) / previous * 100, 'weekly snapshots')
            if previous > 0 else Metric(None, 'not_applicable', 'previous stars zero'))
    else:
        source = ('previous snapshot missing' if not snapshot else
                  'snapshot observation outside boundary tolerance' if not usable else
                  'current stars unavailable' if current is None else 'previous stars unavailable')
        candidate.metrics['star_delta'] = Metric(source=source)
        candidate.metrics['star_growth'] = Metric(source=source)
