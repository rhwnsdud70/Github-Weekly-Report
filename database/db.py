from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Database:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')
        self.connection.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        self.connection.commit()

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self

    def rows(self, sql, params=()):
        return [dict(row) for row in self.connection.execute(sql, params)]

    def repository(self, identity, data):
        self.connection.execute('INSERT INTO repositories VALUES (?,?,?,?,?) ON CONFLICT(identity) '
            'DO UPDATE SET github_id=excluded.github_id, full_name=excluded.full_name, '
            'canonical_url=excluded.canonical_url, data=excluded.data',
            (identity, data.get('github_id'), data['full_name'], data['url'], encode(data)))

    def get_repository(self, identity):
        rows = self.rows('SELECT data FROM repositories WHERE identity=?', (identity,))
        return json.loads(rows[0]['data']) if rows else None

    def snapshot(self, identity, end, data):
        self.connection.execute('INSERT OR REPLACE INTO weekly_snapshots VALUES (?,?,?,?,?,?)',
            (identity, end, data['measured_at'], data.get('stars'), data.get('forks'), encode(data)))

    def previous_snapshot(self, identity, exact_end):
        rows = self.rows('SELECT data FROM weekly_snapshots WHERE identity=? AND period_end=?',
                         (identity, exact_end))
        return json.loads(rows[0]['data']) if rows else None

    def metrics(self, identity, end, metrics, score):
        delta = metrics.get('star_delta', {'value': None, 'status': 'unknown'})
        self.connection.execute('INSERT OR REPLACE INTO weekly_metrics VALUES (?,?,?,?,?,?)',
            (identity, end, delta['value'], delta['status'], encode(metrics), encode(score)))

    def history(self, identity, end, rank, score, data):
        self.connection.execute('INSERT OR REPLACE INTO report_history VALUES (?,?,?,?,?)',
            (identity, end, rank, score, encode(data)))

    def previous_reports(self, identity, before):
        rows = self.rows('SELECT * FROM report_history WHERE identity=? AND period_end<? '
                         'ORDER BY period_end DESC', (identity, before))
        for row in rows:
            row['data'] = json.loads(row['data'])
        return rows

    def save_watchlist(self, end, entries):
        from config.rules import WATCHLIST_LIMIT
        if len(entries) > WATCHLIST_LIMIT:
            raise ValueError('Watchlist exceeds configured limit')
        self.connection.execute('DELETE FROM watchlist WHERE period_end=?', (end,))
        for identity, data in entries:
            self.connection.execute('INSERT INTO watchlist VALUES (?,?,?)', (identity, end, encode(data)))

    def was_watched(self, identity, end):
        return bool(self.rows('SELECT 1 FROM watchlist WHERE identity=? AND period_end=?', (identity, end)))

    def start_run(self, start, end, mode):
        run_id = str(uuid4())
        self.connection.execute('INSERT INTO automation_runs '
            '(run_id,period_start,period_end,mode,status,started_at) VALUES (?,?,?,?,?,?)',
            (run_id, start, end, mode, 'RUNNING', utcnow()))
        self.connection.commit()
        return run_id

    def claim_live_run(self, start, end):
        """Serialize claims so cron/manual overlap cannot duplicate a weekly delivery."""
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            if self.rows('SELECT 1 FROM automation_runs WHERE period_end=? AND mode=?', (end,'live')):
                raise ValueError('This weekly live run already exists; automatic resend is disabled')
            return self.start_run(start,end,'live')
        except Exception:
            self.connection.rollback()
            raise

    def finish_run(self, run_id, status, report=None, error=None):
        self.connection.execute('UPDATE automation_runs SET status=?,finished_at=?,report=?,error=? WHERE run_id=?',
                                (status, utcnow(), encode(report), error, run_id))
        self.connection.commit()

    def log_transmission(self, run_id, kind, rank, part, attempt, status, error=None):
        self.connection.execute('INSERT INTO telegram_logs '
            '(run_id,message_type,top_rank,part_number,attempt_count,status,error_message,sent_time) '
            'VALUES (?,?,?,?,?,?,?,?)', (run_id, kind, rank, part, attempt, status, error, utcnow()))
        self.connection.commit()
