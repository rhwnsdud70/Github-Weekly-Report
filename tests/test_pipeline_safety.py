import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from database.db import Database, DuplicateRunError
from models.serialization import dumps, report_from_dict
from services.persistence_service import persist_week
from services.history_service import attach_history
from services.pipeline import ReportPipeline
from tests.test_formatter import sample_report
from tests.test_ranking import period


def test_atomic_rollback_including_measurements(monkeypatch):
    db = Database(':memory:')
    report = sample_report()
    def fail(*args):
        raise RuntimeError('storage failure')
    monkeypatch.setattr(db,'save_watchlist',fail)
    with pytest.raises(RuntimeError):
        persist_week(db,report.top,report)
    for table in ('repositories','weekly_snapshots','weekly_metrics','report_history'):
        assert db.rows(f'SELECT * FROM {table}') == []


def test_concurrent_weekly_claim(tmp_path):
    path = tmp_path/'report.db'
    Database(path).close()
    barrier = Barrier(2)
    def claim(_):
        db = Database(path)
        try:
            barrier.wait(timeout=5)
            return db.claim_live_run(period().start.isoformat(),period().end.isoformat())
        except DuplicateRunError:
            return 'duplicate'
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim,range(2)))
    assert results.count('duplicate') == 1
    db = Database(path)
    assert len(db.rows('SELECT * FROM automation_runs')) == 1
    with pytest.raises(DuplicateRunError):
        db.claim_live_run(period().start.isoformat(),period().end.isoformat().replace('+00:00','Z'))
    db.close()


def test_report_serialization_roundtrip():
    report = sample_report()
    restored = report_from_dict(json.loads(dumps(report)))
    assert restored == report


def test_late_snapshot_is_not_seven_day_growth():
    from datetime import timedelta
    from dataclasses import asdict
    db = Database(':memory:')
    c = sample_report().top[0]
    db.repository(c.repository.identity,asdict(c.repository))
    db.snapshot(c.repository.identity,period().start.isoformat(),{
        'measured_at':period().start.isoformat(),'stars':100})
    attach_history(c,db,period(),period().end+timedelta(days=2))
    assert c.metric('star_delta').status == 'unknown'
    assert not c.previous['snapshot_usable']


def test_incomplete_live_mode_rejected_before_claim():
    db = Database(':memory:')
    pipeline = ReportPipeline(db,None,None)
    with pytest.raises(ValueError,match='Live mode requires'):
        pipeline.run(period(),mode='live')
    assert db.rows('SELECT * FROM automation_runs') == []
