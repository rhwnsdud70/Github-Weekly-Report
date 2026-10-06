from database.db import Database
from services.persistence_service import persist_week, history_summary
from tests.test_formatter import sample_report


def test_atomic_week_persistence_and_idempotency():
    db = Database(':memory:')
    report = sample_report()
    for _ in range(2):
        persist_week(db,report.top,report,measured_at=report.period.end.isoformat(),transmission='PARTIAL_SUCCESS')
    assert len(db.rows('SELECT * FROM weekly_snapshots')) == 5
    assert len(db.rows('SELECT * FROM report_history')) == 5
    summary = history_summary(db,report.top[0].repository.identity,'2027')
    assert summary['count'] == 1
    assert summary['last_rank'] == 1
    assert db.rows('SELECT star_delta_status FROM weekly_metrics')[0]['star_delta_status'] == 'confirmed'
