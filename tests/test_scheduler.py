from datetime import datetime, timedelta, timezone
import pytest
from services.scheduler import KST, report_period, next_run, scheduler_loop


def test_exact_sunday_boundary_half_open():
    boundary = datetime(2026,10,4,19,tzinfo=KST)
    period = report_period(boundary)
    assert period.end == boundary
    assert (period.end-period.start).total_seconds() == 168*3600
    assert period.contains(period.start)
    assert period.contains(boundary-timedelta(microseconds=1))
    assert not period.contains(boundary)
    assert report_period(boundary-timedelta(seconds=1)).end == boundary-timedelta(days=7)
    assert report_period(boundary.astimezone(timezone.utc)) == period
    assert next_run(boundary) == boundary+timedelta(days=7)


def test_scheduler_clock_injection():
    now = [datetime(2026,10,4,18,59,59,tzinfo=KST)]
    jobs = []
    scheduler_loop(jobs.append,clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+timedelta(seconds=seconds)),
                   stop=lambda:bool(jobs))
    assert len(jobs) == 1
    assert jobs[0].end.hour == 19


@pytest.mark.parametrize('delay_minutes',[0,5,7,12,30,60,61,240,24*60])
def test_actions_delay_keeps_completed_sunday_period(delay_minutes):
    boundary = datetime(2026,10,18,19,tzinfo=KST)
    period = report_period(boundary+timedelta(minutes=delay_minutes))
    assert period.end == boundary
    assert period.start == datetime(2026,10,11,19,tzinfo=KST)
    assert period.end-period.start == timedelta(hours=168)
    assert period.contains(period.start)
    assert not period.contains(period.end)
