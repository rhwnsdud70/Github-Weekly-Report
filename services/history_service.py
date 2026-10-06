import json
from models.domain import Metric


def attach_history(candidate, database, period):
    identity = candidate.repository.identity
    snapshot = database.previous_snapshot(identity, period.start.isoformat())
    reports = database.previous_reports(identity, period.end.isoformat())
    score = database.rows('SELECT score FROM weekly_metrics WHERE identity=? AND period_end=?',
                          (identity, period.start.isoformat()))
    candidate.previous = {'snapshot': snapshot, 'reports': reports,
        'score': json.loads(score[0]['score']) if score else None,
        'watchlist': database.was_watched(identity, period.start.isoformat())}
    current = candidate.repository.stars
    previous = snapshot.get('stars') if snapshot else None
    if current is not None and previous is not None:
        candidate.metrics['star_delta'] = Metric.known(current - previous, 'weekly snapshots')
        candidate.metrics['star_growth'] = (Metric.known((current - previous) / previous * 100, 'weekly snapshots')
            if previous > 0 else Metric(None, 'not_applicable', 'previous stars zero'))
    else:
        candidate.metrics['star_delta'] = Metric(source='previous snapshot missing')
        candidate.metrics['star_growth'] = Metric(source='previous snapshot missing')
