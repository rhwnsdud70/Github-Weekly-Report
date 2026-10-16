from contextlib import closing
import hashlib
import sqlite3

import pytest

from database.sqlite_backup import SQLiteBackupError, validate_database
from services.r2_persistence import PRODUCTION_KEY, R2DatabaseStore, R2PersistenceError
from scripts import bootstrap_r2_database as cli
from tests.test_r2_persistence import FakeS3, FakeClientError, seed_database, settings


class BootstrapS3(FakeS3):
    def __init__(self, *args, race=False, corrupt=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.race, self.corrupt = race, corrupt

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch):
        self.record('put')
        assert IfNoneMatch == '*'
        if self.race:
            self.objects[Key] = b'racing creator'
        if Key in self.objects:
            raise FakeClientError('PreconditionFailed',412)
        self.objects[Key] = Body.read()
        if self.corrupt == 'size':
            self.objects[Key] += b'x'
        elif self.corrupt == 'checksum':
            value = self.objects[Key]
            self.objects[Key] = b'x' + value[1:]


def prepared(tmp_path, **kwargs):
    path = tmp_path/'production.db'
    seed_database(path)
    client = BootstrapS3(**kwargs)
    return path, client, R2DatabaseStore(settings(path),client)


def test_bootstrap_preserves_database_backup_claim_and_verifies_remote(tmp_path):
    path, client, store = prepared(tmp_path)
    original = path.read_bytes()
    result = store.bootstrap()
    assert path.read_bytes() == original
    backup = result['local_backup']
    validate_database(backup)
    assert backup.read_bytes() == client.objects[PRODUCTION_KEY]
    assert result['size'] == len(client.objects[PRODUCTION_KEY])
    assert result['sha256'] == hashlib.sha256(backup.read_bytes()).hexdigest()
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute('SELECT status FROM automation_runs').fetchall() == [('SUCCESS',)]
    assert client.calls == ['get','put','get']
    assert client.body.closed


def test_existing_remote_refused_without_snapshot_or_put(tmp_path):
    path, client, store = prepared(tmp_path,production=b'existing')
    original = path.read_bytes()
    with pytest.raises(R2PersistenceError,match='REMOTE_OBJECT_EXISTS'):
        store.bootstrap()
    assert client.calls == ['get']
    assert client.body.closed
    assert client.objects[PRODUCTION_KEY] == b'existing'
    assert path.read_bytes() == original
    assert store.bootstrap_backup_path is None


def test_racing_remote_creator_is_not_overwritten(tmp_path):
    path, client, store = prepared(tmp_path,race=True)
    with pytest.raises(R2PersistenceError,match='REMOTE_OBJECT_EXISTS'):
        store.bootstrap()
    assert client.objects[PRODUCTION_KEY] == b'racing creator'
    validate_database(store.bootstrap_backup_path)
    assert path.exists()


@pytest.mark.parametrize('error',[
    FakeClientError(), FakeClientError('NoSuchBucket',404),
    FakeClientError('404',404), OSError('sensitive endpoint/key'),
])
def test_auth_network_ambiguous_404_not_treated_as_absence(tmp_path,error):
    path, client, store = prepared(tmp_path,fail={'get':error})
    with pytest.raises(R2PersistenceError) as caught:
        store.bootstrap()
    assert 'sensitive' not in str(caught.value).lower()
    assert client.calls == ['get']
    assert store.bootstrap_backup_path is None


@pytest.mark.parametrize('corrupt',['size','checksum'])
def test_remote_verification_failure_preserves_local_backup(tmp_path,corrupt):
    path, client, store = prepared(tmp_path,corrupt=corrupt)
    with pytest.raises(R2PersistenceError,match='MISMATCH'):
        store.bootstrap()
    validate_database(path)
    validate_database(store.bootstrap_backup_path)
    assert client.body.closed


def test_upload_failure_keeps_backup(tmp_path):
    path, client, store = prepared(tmp_path,fail={'put':OSError('secret')})
    with pytest.raises(R2PersistenceError,match='NETWORK_ERROR'):
        store.bootstrap()
    validate_database(store.bootstrap_backup_path)
    assert PRODUCTION_KEY not in client.objects
    assert path.exists()


@pytest.mark.parametrize('mode',['integration-test','preview','dry-run'])
def test_nonproduction_content_rejected_before_network(tmp_path,mode):
    path = tmp_path/'production.db'
    seed_database(path,mode)
    client = BootstrapS3()
    with pytest.raises(SQLiteBackupError):
        R2DatabaseStore(settings(path),client).bootstrap()
    assert client.calls == []


def test_fixture_metrics_rejected_before_network(tmp_path):
    path, client, store = prepared(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE weekly_metrics SET metrics=?",('{"stars":{"source":"fixture"}}',))
        connection.commit()
    with pytest.raises(SQLiteBackupError):
        store.bootstrap()
    assert client.calls == []


@pytest.mark.parametrize('bad',['missing','corrupt','schema'])
def test_invalid_database_blocked_before_network(tmp_path,bad):
    path = tmp_path/'production.db'
    if bad == 'corrupt':
        path.write_bytes(b'bad'*100)
    elif bad == 'schema':
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE unrelated (id INTEGER)')
            connection.commit()
    client = BootstrapS3()
    with pytest.raises(SQLiteBackupError):
        R2DatabaseStore(settings(path),client).bootstrap()
    assert client.calls == []


def test_wal_committed_data_exported_and_handles_closed(tmp_path):
    path, client, store = prepared(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE repositories SET full_name='owner/updated'")
        writer.commit()
        store.bootstrap()
        assert not path.with_name(path.name+'-wal').exists() or path.with_name(path.name+'-wal').stat().st_size == 0
    with closing(sqlite3.connect(store.bootstrap_backup_path)) as connection:
        assert connection.execute('SELECT full_name FROM repositories').fetchone() == ('owner/updated',)


def test_busy_wal_blocks_upload(tmp_path):
    path, client, store = prepared(tmp_path)
    with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM repositories').fetchall()
        writer.execute("UPDATE repositories SET full_name='owner/updated'")
        writer.commit()
        with pytest.raises(SQLiteBackupError):
            store.bootstrap()
        reader.rollback()
    assert client.calls == ['get']
    assert path.exists()


def test_cli_success_offline_and_no_pipeline(tmp_path,monkeypatch,capsys):
    path, client, store = prepared(tmp_path)
    monkeypatch.setattr(cli.Settings,'load',lambda _:settings(path))
    monkeypatch.setattr(cli,'R2DatabaseStore',lambda _:store)
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert 'SUCCESS' in output and 'SHA256 verified:' in output
    for secret in ('dummy-access','dummy-secret','mock-bucket'):
        assert secret not in output


def test_cli_failure_sanitized(tmp_path,monkeypatch,capsys):
    path, client, store = prepared(tmp_path,fail={'put':OSError('dummy-secret')})
    monkeypatch.setattr(cli.Settings,'load',lambda _:settings(path))
    monkeypatch.setattr(cli,'R2DatabaseStore',lambda _:store)
    assert cli.main([]) == 1
    output = capsys.readouterr().out
    assert 'NETWORK_ERROR' in output and 'Local backup retained' in output
    assert 'dummy-secret' not in output


def test_no_force_option():
    with pytest.raises(SystemExit):
        cli.main(['--force'])
