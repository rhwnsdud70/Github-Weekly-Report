from datetime import datetime, timedelta, timezone
from database.db import Database
from scripts.run_full_top5_live import existing_claim, existing_live_claim, manual_period
from services.scheduler import report_period


KST = timezone(timedelta(hours=9))


def test_manual_period_override_is_exact_half_open_seven_days():
    period = manual_period('2026-10-11T19:00:00+09:00')
    assert period.start.isoformat() == '2026-10-04T19:00:00+09:00'
    assert period.end.isoformat() == '2026-10-11T19:00:00+09:00'
    assert period.end-period.start == timedelta(days=7)
    assert period.contains(period.start)
    assert not period.contains(period.end)


def test_existing_live_claim_is_detected_without_modification():
    database = Database(':memory:')
    period = manual_period('2026-10-11T19:00:00+09:00')
    assert not existing_live_claim(database,period)
    run_id = database.start_run(period.start.isoformat(),period.end.isoformat(),'live')
    assert existing_live_claim(database,period)
    assert database.rows('SELECT run_id,status FROM automation_runs') == [
        {'run_id':run_id,'status':'RUNNING'}]


def test_default_period_is_unchanged_scheduler_period():
    now = datetime(2026,10,10,12,tzinfo=KST)
    assert manual_period(now=now) == report_period(now)


def test_integration_claim_is_independent_from_live_claim():
    database = Database(':memory:')
    selected_period = manual_period('2026-10-11T19:00:00+09:00')
    database.claim_run(selected_period.start.isoformat(),selected_period.end.isoformat(),'integration-test')
    assert existing_claim(database,selected_period,'integration-test')
    assert not existing_live_claim(database,selected_period)
