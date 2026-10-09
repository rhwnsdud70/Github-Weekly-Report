import json
from models.domain import Metric, instant
from config.rules import SNAPSHOT_BOUNDARY_TOLERANCE_SECONDS


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
    if observed_at is not None:
        current_offset = (instant(observed_at)-period.end).total_seconds()
        previous_offset = ((instant(snapshot['measured_at'])-period.start).total_seconds()
                           if snapshot else None)
        usable = (0 <= current_offset <= SNAPSHOT_BOUNDARY_TOLERANCE_SECONDS and
                  previous_offset is not None and 0 <= previous_offset <= SNAPSHOT_BOUNDARY_TOLERANCE_SECONDS)
    candidate.previous['snapshot_usable'] = usable
    current = candidate.repository.stars
    previous = snapshot.get('stars') if snapshot and usable else None
    if current is not None and previous is not None:
        candidate.metrics['star_delta'] = Metric.known(current - previous, 'weekly snapshots')
        candidate.metrics['star_growth'] = (Metric.known((current - previous) / previous * 100, 'weekly snapshots')
            if previous > 0 else Metric(None, 'not_applicable', 'previous stars zero'))
    else:
        candidate.metrics['star_delta'] = Metric(source='previous snapshot missing')
        candidate.metrics['star_growth'] = Metric(source='previous snapshot missing')
