from dataclasses import asdict
from database.db import utcnow
from models.domain import WeeklySnapshot


def persist_measurements(database, candidates, period, measured_at=None):
    end = period.end.isoformat()
    measured_at = measured_at or utcnow()
    with database.transaction():
        for candidate in candidates:
            repo = candidate.latest_repository or candidate.repository
            database.repository(repo.identity, asdict(repo))
            snapshot = WeeklySnapshot(repo.identity,end,candidate.latest_observed_at or measured_at,repo.stars,repo.forks,
                candidate.metric('issues').value,candidate.metric('pulls').value,candidate.metric('discussions').value)
            database.snapshot(repo.identity,end,{**asdict(snapshot),
                'discussion_total':candidate.metric('discussion_total').value})
            database.metrics(repo.identity,end,{key:asdict(value) for key,value in candidate.metrics.items()},
                             candidate.score.to_dict() if candidate.score else {})


def persist_week(database, candidates, report, measured_at=None, transmission=None):
    end = report.period.end.isoformat()
    with database.transaction():
        persist_measurements(database,candidates,report.period,measured_at)
        for rank, candidate in enumerate(report.top,1):
            repo = candidate.latest_repository or candidate.repository
            database.repository(repo.identity,asdict(repo))
            database.history(repo.identity,end,rank,candidate.score.total,{
                'repository':asdict(repo), 'grade':candidate.score.grade,
                'selection_reasons':candidate.selection_reasons,
                'events':[asdict(event) for event in candidate.events],
                'event_ids':[list(event.identity) for event in candidate.events],
                'changes_since_report':candidate.changes_since_report,
                'narrative':candidate.narrative, 'evidence':candidate.evidence,
                'observation':candidate.observation,
                'transmission_status':transmission})
        database.save_watchlist(end,[(c.repository.identity,{
            'name':c.repository.full_name,'url':c.repository.url,'stars':(c.latest_repository or c.repository).stars,
            'score':c.score.to_dict(),'signals':sorted(c.signals),
            'reason':'strong signals below final TOP 5',
            'next_check':'growth persistence, new release/security event'}) for c in report.watchlist])


def history_summary(database, identity, before):
    reports = database.previous_reports(identity,before)
    if not reports:
        return None
    return {'first_reported':reports[-1]['period_end'],'last_reported':reports[0]['period_end'],
            'count':len(reports),'last_rank':reports[0]['rank'],'last_score':reports[0]['score'],
            'last_reasons':reports[0]['data'].get('selection_reasons',[])}
