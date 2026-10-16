"""Validate and export a standalone SQLite database, including committed WAL frames."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


class SQLiteBackupError(ValueError):
    """Safe diagnostic; SQLite error text and database contents are not exposed."""


REQUIRED_COLUMNS = {
    'repositories': {'identity','github_id','full_name','canonical_url','data'},
    'automation_runs': {'run_id','period_start','period_end','mode','status',
                        'started_at','finished_at','error','report'},
    'weekly_snapshots': {'identity','period_end','measured_at','stars','forks','data'},
    'weekly_metrics': {'identity','period_end','star_delta','star_delta_status','metrics','score'},
    'report_history': {'identity','period_end','rank','score','data'},
    'watchlist': {'identity','period_end','data'},
    'telegram_logs': {'id','run_id','message_type','top_rank','part_number','attempt_count',
                      'status','error_message','sent_time'},
}


def validate_connection(connection, production=True):
    """Check physical integrity, references, required schema and test contamination."""
    try:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise SQLiteBackupError('SQLite integrity check failed')
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise SQLiteBackupError('SQLite foreign key check failed')
        for table,columns in REQUIRED_COLUMNS.items():
            actual = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if not columns <= actual:
                raise SQLiteBackupError('SQLite application schema mismatch')
        if production:
            if connection.execute("SELECT 1 FROM automation_runs WHERE mode <> 'live' LIMIT 1").fetchone():
                raise SQLiteBackupError('Non-production run found; production transfer blocked')
            for (data,) in connection.execute('SELECT data FROM weekly_snapshots'):
                if json.loads(data).get('fixture'):
                    raise SQLiteBackupError('Fixture snapshot found; production transfer blocked')
            for (data,) in connection.execute('SELECT metrics FROM weekly_metrics'):
                if any('fixture' in str(metric.get('source','')).lower()
                       for metric in json.loads(data).values()):
                    raise SQLiteBackupError('Fixture metric found; production transfer blocked')
    except (sqlite3.Error,ValueError,TypeError,AttributeError) as error:
        if isinstance(error,SQLiteBackupError):
            raise
        raise SQLiteBackupError('SQLite validation failed') from None


def validate_database(path, production=True):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 100:
        raise SQLiteBackupError('SQLite file missing or empty')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as connection:
            validate_connection(connection,production)
    except sqlite3.Error:
        raise SQLiteBackupError('SQLite database could not be read') from None


def checkpoint_and_close(connection):
    """Close a committed connection, rejecting busy WALs and pending transactions.

    Callers must finish their transactions first. An uncommitted connection is left
    open so its owner can explicitly commit or roll back; no implicit commit occurs.
    """
    if connection.in_transaction:
        raise SQLiteBackupError('Commit or roll back pending SQLite transaction before export')
    try:
        result = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if result[0] != 0:
            raise SQLiteBackupError('SQLite WAL checkpoint busy; export blocked')
    except sqlite3.Error:
        raise SQLiteBackupError('SQLite WAL checkpoint failed') from None
    finally:
        connection.close()


def snapshot_database(source, destination):
    """Checkpoint the source, close handles, then use SQLite backup for a coherent file.

    Application writers must be stopped before calling. The backup API also protects
    against a torn file copy. Destination uses DELETE journal mode and needs no WAL.
    """
    validate_database(source)
    try:
        connection = sqlite3.connect(Path(source).resolve().as_uri()+'?mode=rw',uri=True,timeout=1)
        checkpoint_and_close(connection)
        with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as reader:
            with closing(sqlite3.connect(destination)) as snapshot:
                reader.backup(snapshot)
                snapshot.execute('PRAGMA journal_mode=DELETE')
                validate_connection(snapshot)
    except sqlite3.Error:
        raise SQLiteBackupError('SQLite snapshot export failed') from None
