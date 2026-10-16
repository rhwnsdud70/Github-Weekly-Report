from dataclasses import replace
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from config.settings import Settings
from database.db import Database
from database.sqlite_backup import SQLiteBackupError, checkpoint_and_close, validate_database
from services.r2_persistence import (
    BACKUP_PREFIX, PRODUCTION_KEY, R2DatabaseStore, R2PersistenceError, create_r2_client,
)


class FakeClientError(Exception):
    def __init__(self, code='AccessDenied', status=403):
        super().__init__('Sensitive response/access key/URL must never be printed')
        self.response = {'Error':{'Code':code,'Message':str(self)},
                         'ResponseMetadata':{'HTTPStatusCode':status}}


class FakeS3:
    def __init__(self, production=None, fail=None):
        self.objects = {} if production is None else {PRODUCTION_KEY:production}
        self.calls = []
        self.fail = fail or {}
        self.body = None

    def record(self, operation):
        self.calls.append(operation)
        if operation in self.fail:
            raise self.fail[operation]

    def get_object(self, Bucket, Key):
        self.record('get')
        if Key not in self.objects:
            raise FakeClientError('NoSuchKey',404)
        self.body = io.BytesIO(self.objects[Key])
        return {'Body':self.body,'ContentLength':len(self.objects[Key])}

    def copy_object(self, Bucket, Key, CopySource):
        self.record('copy')
        assert CopySource['Bucket'] == Bucket
        if CopySource['Key'] not in self.objects:
            raise FakeClientError('NoSuchKey',404)
        self.objects[Key] = self.objects[CopySource['Key']]
        return {'CopyObjectResult':{'ETag':'mock'}}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.record('put')
        assert Key == PRODUCTION_KEY
        assert ContentType == 'application/vnd.sqlite3'
        self.objects[Key] = Body.read()


def settings(path):
    return Settings(database_path=str(path),r2_endpoint='https://account.r2.cloudflarestorage.com',
                    r2_access_key_id='dummy-access',r2_secret_access_key='dummy-secret',r2_bucket='mock-bucket')


def seed_database(path, mode='live'):
    database = Database(path)
    identity = 'id:1'
    database.repository(identity,{'github_id':1,'full_name':'owner/project',
                                 'url':'https://github.com/owner/project'})
    database.connection.commit()
    run = database.start_run('start','end',mode)
    database.snapshot(identity,'end',{'measured_at':'end','stars':100,'forks':4})
    database.metrics(identity,'end',{'star_delta':{'value':None,'status':'unknown'}},{'total':10})
    database.history(identity,'end',1,10,{'narrative':'saved'})
    database.save_watchlist('end',[(identity,{'name':'owner/project'})])
    database.log_transmission(run,'top',1,1,1,'SUCCESS')
    database.finish_run(run,'SUCCESS',{'top':['owner/project']})
    database.close()
    return Path(path).read_bytes()


def test_roundtrip_preserves_all_application_tables_and_claim(tmp_path):
    source = tmp_path/'source'/'production.db'
    source.parent.mkdir()
    seed_database(source)
    client = FakeS3()
    assert R2DatabaseStore(settings(source),client).upload() is None
    assert client.calls == ['copy','put']
    target = tmp_path/'restored'/'production.db'
    assert R2DatabaseStore(settings(target),client).restore() is True
    assert client.body.closed
    validate_database(target)
    for path in (source,target):
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute('SELECT status FROM automation_runs').fetchall() == [('SUCCESS',)]
            assert connection.execute('SELECT star_delta,star_delta_status FROM weekly_metrics').fetchone() == (None,'unknown')
            for table in ('repositories','weekly_snapshots','report_history','watchlist','telegram_logs'):
                assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone() == (1,)


def test_backup_contains_previous_remote_not_new_local_db(tmp_path):
    path = tmp_path/'production.db'
    seed_database(path)
    old = b'previous remote version'
    client = FakeS3(old)
    store = R2DatabaseStore(settings(path),client)
    key = store.upload()
    assert key.startswith(BACKUP_PREFIX) and key.endswith('.db')
    assert client.objects[key] == old
    assert client.objects[PRODUCTION_KEY] != old
    second_key = store.upload()
    assert second_key != key
    assert client.calls == ['copy','put','copy','put']


def test_missing_object_requires_explicit_database_initialization(tmp_path):
    path = tmp_path/'production.db'
    client = FakeS3()
    assert R2DatabaseStore(settings(path),client).restore() is False
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('code,status,category',[
    ('AccessDenied',403,'AUTHENTICATION_OR_PERMISSION'),
    ('InvalidAccessKeyId',403,'AUTHENTICATION_OR_PERMISSION'),
    ('SignatureDoesNotMatch',403,'AUTHENTICATION_OR_PERMISSION'),
    ('NoSuchKey',403,'AUTHENTICATION_OR_PERMISSION'),
    ('NoSuchBucket',404,'BUCKET_NOT_FOUND'),
    ('404',404,'HTTP_ERROR'),  # Ambiguous 404 is NOT first run.
    ('InternalError',500,'HTTP_ERROR'),
    ('SlowDown',429,'RATE_LIMIT'),
])
def test_remote_errors_are_not_missing_objects(tmp_path,code,status,category):
    path = tmp_path/'production.db'
    client = FakeS3(fail={'get':FakeClientError(code,status)})
    with pytest.raises(R2PersistenceError) as raised:
        R2DatabaseStore(settings(path),client).restore()
    assert raised.value.category == category
    assert raised.value.http_status == status
    assert 'Sensitive' not in str(raised.value)
    assert not path.exists()


def test_network_failure_does_not_trigger_first_run(tmp_path):
    client = FakeS3(fail={'get':OSError('secret-key embedded in endpoint')})
    with pytest.raises(R2PersistenceError,match='NETWORK_ERROR') as raised:
        R2DatabaseStore(settings(tmp_path/'production.db'),client).restore()
    assert 'secret-key' not in str(raised.value)


def test_stream_failure_closes_body_and_leaves_no_partial_database(tmp_path):
    class BrokenStream(io.BytesIO):
        def read(self,*args):
            raise OSError('secret endpoint details')
    client = FakeS3()
    client.body = BrokenStream(b'partial download')
    client.get_object = lambda **kwargs: {'Body':client.body}
    path = tmp_path/'production.db'
    with pytest.raises(R2PersistenceError,match='NETWORK_ERROR'):
        R2DatabaseStore(settings(path),client).restore()
    assert client.body.closed
    assert not path.exists()
    assert not list(tmp_path.glob('.r2-*'))


def test_restore_rejects_remote_integration_database(tmp_path):
    source = tmp_path/'integration.db'
    content = seed_database(source,mode='integration-test')
    target = tmp_path/'production.db'
    with pytest.raises(SQLiteBackupError,match='Non-production run'):
        R2DatabaseStore(settings(target),FakeS3(content)).restore()
    assert not target.exists()


def test_missing_local_upload_never_creates_database_or_contacts_r2(tmp_path):
    path = tmp_path/'absent-folder'/'production.db'
    client = FakeS3()
    with pytest.raises(SQLiteBackupError,match='missing'):
        R2DatabaseStore(settings(path),client).upload()
    assert not path.exists()
    assert client.calls == []


def test_backup_failure_blocks_production_overwrite(tmp_path):
    path = tmp_path/'production.db'
    seed_database(path)
    client = FakeS3(b'old',fail={'copy':FakeClientError()})
    with pytest.raises(R2PersistenceError,match='backup'):
        R2DatabaseStore(settings(path),client).upload()
    assert client.calls == ['copy']
    assert client.objects[PRODUCTION_KEY] == b'old'
    assert not list(tmp_path.glob('.r2-*'))


def test_upload_failure_keeps_backup(tmp_path):
    path = tmp_path/'production.db'
    seed_database(path)
    client = FakeS3(b'old',fail={'put':TimeoutError('secret-key')})
    with pytest.raises(R2PersistenceError,match='upload'):
        R2DatabaseStore(settings(path),client).upload()
    assert client.objects[PRODUCTION_KEY] == b'old'
    assert [body for key,body in client.objects.items() if key.startswith(BACKUP_PREFIX)] == [b'old']


@pytest.mark.parametrize('content',[b'',b'not sqlite',b'SQLite format 3\x00'+b'x'*200])
def test_corrupt_download_is_never_published(tmp_path,content):
    path = tmp_path/'production.db'
    client = FakeS3(content)
    with pytest.raises(SQLiteBackupError):
        R2DatabaseStore(settings(path),client).restore()
    assert client.body.closed
    assert not path.exists()
    assert not list(tmp_path.glob('.r2-*'))


def test_existing_local_database_or_sidecars_are_not_overwritten(tmp_path):
    path = tmp_path/'production.db'
    client = FakeS3()
    original = seed_database(path)
    with pytest.raises(SQLiteBackupError,match='already exists'):
        R2DatabaseStore(settings(path),client).restore()
    assert path.read_bytes() == original
    assert client.calls == []
    path.unlink()
    Path(str(path)+'-wal').write_bytes(b'pending WAL')
    with pytest.raises(SQLiteBackupError):
        R2DatabaseStore(settings(path),client).restore()
    assert client.calls == []


def test_wal_export_includes_committed_rows_and_closes_connections(tmp_path):
    path = tmp_path/'production.db'
    seed_database(path)
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute("UPDATE repositories SET full_name='owner/renamed'")
    writer.commit()
    assert Path(str(path)+'-wal').stat().st_size > 0
    client = FakeS3()
    R2DatabaseStore(settings(path),client).upload()
    writer.close()
    restored = tmp_path/'restored.db'
    restored.write_bytes(client.objects[PRODUCTION_KEY])
    with closing(sqlite3.connect(restored)) as database:
        assert database.execute('SELECT full_name FROM repositories').fetchone() == ('owner/renamed',)
        assert database.execute('PRAGMA journal_mode').fetchone() == ('delete',)
    assert not Path(str(restored)+'-wal').exists()
    restored.unlink()  # Windows also confirms there is no lingering export connection.


def test_checkpoint_rejects_uncommitted_and_busy_wal(tmp_path):
    path = tmp_path/'production.db'
    seed_database(path)
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute("UPDATE repositories SET full_name='uncommitted'")
    with pytest.raises(SQLiteBackupError,match='pending'):
        checkpoint_and_close(writer)
    assert writer.in_transaction  # The helper must not silently commit or discard it.
    client = FakeS3()
    with pytest.raises(SQLiteBackupError,match='busy'):
        R2DatabaseStore(settings(path),client).upload()
    assert client.calls == []
    writer.rollback()
    checkpoint_and_close(writer)
    with pytest.raises(sqlite3.ProgrammingError):
        writer.execute('SELECT 1')


@pytest.mark.parametrize('mode',['integration-test','dry-run','preview','telegram-test'])
def test_renamed_test_database_cannot_be_uploaded_to_production(tmp_path,mode):
    path = tmp_path/'production.db'
    seed_database(path,mode=mode)
    client = FakeS3()
    with pytest.raises(SQLiteBackupError,match='Non-production run'):
        R2DatabaseStore(settings(path),client).upload()
    assert client.calls == []


def test_integration_filename_is_rejected_before_remote_access(tmp_path):
    with pytest.raises(SQLiteBackupError,match='Non-production database path'):
        R2DatabaseStore(settings(tmp_path/'full_integration_test.db'),FakeS3())


def test_fixture_snapshot_or_metric_cannot_be_uploaded(tmp_path):
    path = tmp_path/'production.db'
    seed_database(path)
    client = FakeS3()
    with closing(sqlite3.connect(path)) as database, database:
        database.execute('UPDATE weekly_snapshots SET data=?',(json.dumps({'fixture':'integration-test'}),))
    with pytest.raises(SQLiteBackupError,match='Fixture snapshot'):
        R2DatabaseStore(settings(path),client).upload()
    with closing(sqlite3.connect(path)) as database, database:
        database.execute('UPDATE weekly_snapshots SET data=?',('{}',))
        database.execute('UPDATE weekly_metrics SET metrics=?',
                         (json.dumps({'trending':{'source':'INTEGRATION TEST FIXTURE'}}),))
    with pytest.raises(SQLiteBackupError,match='Fixture metric'):
        R2DatabaseStore(settings(path),client).upload()
    assert client.calls == []


def test_corrupt_or_incompatible_local_database_never_reaches_r2(tmp_path):
    path = tmp_path/'production.db'
    path.write_bytes(b'corrupt')
    client = FakeS3()
    with pytest.raises(SQLiteBackupError):
        R2DatabaseStore(settings(path),client).upload()
    path.unlink()
    with closing(sqlite3.connect(path)) as database, database:
        database.execute('CREATE TABLE unrelated(id INTEGER)')
    with pytest.raises(SQLiteBackupError,match='schema'):
        R2DatabaseStore(settings(path),client).upload()
    assert client.calls == []


def test_foreign_key_corruption_blocks_upload(tmp_path):
    path = tmp_path/'production.db'
    seed_database(path)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("UPDATE weekly_metrics SET identity='missing'")
    with pytest.raises(SQLiteBackupError,match='foreign key'):
        R2DatabaseStore(settings(path),FakeS3()).upload()


def test_r2_settings_sdk_configuration_and_secret_free_output(tmp_path,monkeypatch,caplog):
    env = tmp_path/'example.env'
    env.write_text('R2_ENDPOINT=https://account.r2.cloudflarestorage.com\n'
                   'R2_ACCESS_KEY_ID=dummy-access\nR2_SECRET_ACCESS_KEY=dummy-secret\nR2_BUCKET=mock-bucket\n')
    configured = Settings.load(env,environ={})
    assert configured.r2_access_key_id == 'dummy-access'
    assert configured.r2_secret_access_key == 'dummy-secret'
    assert 'dummy-access' not in repr(configured) and 'dummy-secret' not in repr(configured)
    captured = {}
    client = SimpleNamespace(close=lambda:captured.update(closed=True))
    def factory(service,**kwargs):
        captured.update(service=service,**kwargs)
        return client
    monkeypatch.setitem(sys.modules,'boto3',SimpleNamespace(client=factory))
    monkeypatch.setitem(sys.modules,'botocore.config',SimpleNamespace(Config=lambda **kwargs:kwargs))
    assert create_r2_client(configured) is client
    assert captured['region_name'] == 'auto'
    assert captured['config']['signature_version'] == 's3v4'
    assert captured['config']['s3']['addressing_style'] == 'path'
    with R2DatabaseStore(configured) as store:
        assert store.client is client
    assert captured['closed']
    assert 'dummy-access' not in caplog.text and 'dummy-secret' not in caplog.text


def test_r2_configuration_is_opt_in_and_validated():
    Settings().tz
    with pytest.raises(ValueError,match='R2_ENDPOINT'):
        Settings().validate_r2()
    with pytest.raises(ValueError,match='HTTPS'):
        replace(settings('production.db'),r2_endpoint='https://user:password@host').validate_r2()
