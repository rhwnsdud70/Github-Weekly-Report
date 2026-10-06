import sqlite3
import pytest
from database.db import Database


def test_persistence_and_unknown(tmp_path):
    path = tmp_path / 'report.db'
    db = Database(path)
    with db.transaction():
        db.repository('id:1', {'github_id': 1, 'full_name': 'a/b', 'url': 'https://github.com/a/b'})
        db.snapshot('id:1', 'week', {'measured_at': 'now', 'stars': 0})
        db.metrics('id:1', 'week', {'star_delta': {'value': None, 'status': 'unknown'}}, {})
    db.close()
    db = Database(path)
    assert db.get_repository('id:1')['full_name'] == 'a/b'
    assert db.previous_snapshot('id:1', 'week')['stars'] == 0
    assert db.previous_snapshot('id:1', 'missing') is None
    assert db.rows('SELECT star_delta FROM weekly_metrics')[0]['star_delta'] is None
    with pytest.raises(sqlite3.IntegrityError):
        db.metrics('id:1', 'bad', {'star_delta': {'value': 0, 'status': 'unknown'}}, {})
    db.connection.rollback()
    db.close()


def test_identity_rename_history_and_logs():
    db = Database(':memory:')
    for name in ['old/name', 'new/name']:
        db.repository('id:5', {'github_id': 5, 'full_name': name, 'url': 'https://github.com/' + name})
    assert len(db.rows('SELECT * FROM repositories')) == 1
    db.history('id:5', '2026-01-01', 1, 50, {'events': []})
    assert len(db.previous_reports('id:5', '2026-01-08')) == 1
    run = db.start_run('a', 'b', 'dry-run')
    db.log_transmission(run, 'top', 1, 1, 1, 'SUCCESS')
    db.finish_run(run, 'SUCCESS')
    assert db.rows('SELECT status FROM automation_runs')[0]['status'] == 'SUCCESS'
    db.close()
