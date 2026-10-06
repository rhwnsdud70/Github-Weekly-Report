from datetime import datetime, timedelta, timezone
from models.domain import Candidate, Repository, Event, ScoreResult, ReportPeriod
from services.duplicate_service import merge_candidates, allow_repeat


def test_merge_events_keep_max_not_sum():
    a = Candidate(Repository(1,'a/b','url'), {'trending'}, [Event('major','a','u1','date','release')], score=ScoreResult({'x':50}))
    b = Candidate(Repository(1,'renamed/b','url2'), {'security'}, [Event('high','b','u2','date','security')], score=ScoreResult({'x':30}))
    result = merge_candidates([a,b])
    assert len(result) == 1
    assert result[0].score.total == 50
    assert len(result[0].events) == 2
    assert result[0].collection_sources == {'trending','security'}
    assert len(a.events) == 1
    other = Candidate(Repository(2,'other/b','url3'))
    assert len(merge_candidates([a, other])) == 2


def test_repeat_new_event_only():
    end = datetime(2026,1,11,tzinfo=timezone.utc)
    period = ReportPeriod(end-timedelta(days=7), end)
    c = Candidate(Repository(1,'a/b','url'), previous={'reports':[{
        'period_end':period.start.isoformat(), 'data':{'event_ids':[['major','old']]}}]})
    assert not allow_repeat(c, period)
    c.events = [Event('major','new release','new',(end-timedelta(days=1)).isoformat(),'release')]
    assert allow_repeat(c, period)
    assert c.changes_since_report
    c.events[0].url = 'old'
    assert not allow_repeat(c, period)
