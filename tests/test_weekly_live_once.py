from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import httpx
import pytest

from config import rules
from database.db import Database
from database.sqlite_backup import SQLiteBackupError
from scripts.run_weekly_live_once import parser
from services.demo import DemoBackend
from services.pipeline import ReportPipeline
from services.r2_persistence import PRODUCTION_KEY, BACKUP_PREFIX, R2DatabaseStore
from services.scheduler import KST, report_period
from services.weekly_live_runner import run_weekly_live_once
from tests.test_r2_persistence import FakeS3, FakeClientError, settings as r2_settings


NOW = datetime(2026,10,11,19,12,tzinfo=KST)


def live_settings(path):
    return replace(r2_settings(path),github_token='dummy-github',openai_api_key='dummy-openai',
                   telegram_bot_token='dummy-telegram',telegram_chat_id='dummy-chat')


def empty_remote(tmp_path,claim_status=None):
    path = tmp_path/'remote.db'
    database = Database(path)
    if claim_status:
        period = report_period(NOW)
        run_id = database.claim_live_run(period.start.isoformat(),period.end.astimezone(timezone.utc).isoformat())
        if claim_status != 'RUNNING':
            database.finish_run(run_id,claim_status)
    database.close()
    return path.read_bytes()


def terminal_factory(status='SUCCESS',error=None,after_finish=None):
    @contextmanager
    def factory(database):
        class Pipeline:
            def run(self,period,**kwargs):
                assert kwargs['mode'] == 'live'
                assert kwargs['limit'] == rules.CANDIDATE_LIMIT
                assert set(kwargs) == {'mode','limit','output'}  # No fixtures/cache/period override.
                run_id = database.claim_live_run(period.start.isoformat(),period.end.isoformat())
                database.finish_run(run_id,status)
                if after_finish:
                    after_finish(database)
                if error:
                    raise error
                return {'mode':'live','status':status,'run_id':run_id}
        yield Pipeline()
    return factory


def execute(tmp_path,client,**kwargs):
    configured = live_settings(tmp_path/'local'/'production.db')
    return run_weekly_live_once(configured,store=R2DatabaseStore(configured,client),
                                clock=lambda:NOW,output=tmp_path/'output',**kwargs)


def test_r2_restore_then_real_production_pipeline_then_upload(tmp_path):
    period = report_period(NOW)
    backend = DemoBackend(period,10)
    source = tmp_path/'remote.db'
    database = Database(source)
    backend.seed_history(database)
    database.close()
    original = source.read_bytes()
    remote = FakeS3(original)
    @contextmanager
    def factory(database):
        assert remote.calls == ['get']
        client,github,llm,sender = backend.clients()
        try:
            yield ReportPipeline(database,github,client,llm,sender,clock=lambda:NOW)
        finally:
            client.close()
    result = execute(tmp_path,remote,pipeline_factory=factory)
    assert result.status == 'SUCCESS' and result.exit_code == 0 and result.remote_updated
    assert remote.calls == ['get','copy','put']
    assert remote.objects[PRODUCTION_KEY] != original
    assert [value for key,value in remote.objects.items() if key.startswith(BACKUP_PREFIX)] == [original]
    assert len(backend.deep_repositories) == 5
    assert backend.calls['api.openai.com/v1/responses'] == 10
    assert backend.sent
    assert all('[INTEGRATION TEST]' not in item['text'] for item in backend.sent)
    database = Database(tmp_path/'local'/'production.db')
    assert database.rows('SELECT mode,status FROM automation_runs') == [{'mode':'live','status':'SUCCESS'}]
    assert len(database.rows('SELECT * FROM report_history')) == 5
    database.close()


@pytest.mark.parametrize('status',['SUCCESS','FAILED','RUNNING','PARTIAL_SUCCESS'])
def test_previous_claim_blocks_all_production_clients_without_modification(tmp_path,status):
    original = empty_remote(tmp_path,status)
    remote = FakeS3(original)
    def forbidden(database):
        pytest.fail('Production clients must not be created for an existing claim')
    result = execute(tmp_path,remote,pipeline_factory=forbidden)
    assert result.status == 'SKIPPED_ALREADY_CLAIMED' and result.exit_code == 0
    assert remote.calls == ['get']
    assert remote.objects[PRODUCTION_KEY] == original
    assert (tmp_path/'local'/'production.db').read_bytes() == original


@pytest.mark.parametrize('error',[FakeClientError(),OSError('secret-in-endpoint')])
def test_restore_failure_never_initializes_or_executes_pipeline(tmp_path,error):
    remote = FakeS3(fail={'get':error})
    result = execute(tmp_path,remote,allow_initial_bootstrap=True,
                     pipeline_factory=lambda _:pytest.fail('Pipeline must not run'))
    assert result.status == 'RESTORE_FAILED'
    assert result.exit_code != 0
    assert not (tmp_path/'local'/'production.db').exists()
    assert remote.calls == ['get']


def test_first_run_is_blocked_by_default(tmp_path):
    remote = FakeS3()
    result = execute(tmp_path,remote,pipeline_factory=lambda _:pytest.fail('Pipeline must not run'))
    assert result.status == 'BOOTSTRAP_REQUIRED'
    assert result.exit_code != 0
    assert not (tmp_path/'local'/'production.db').exists()
    assert remote.calls == ['get']


def test_explicit_bootstrap_initializes_production_and_uploads(tmp_path):
    remote = FakeS3()
    result = execute(tmp_path,remote,allow_initial_bootstrap=True,pipeline_factory=terminal_factory())
    assert result.status == 'SUCCESS'
    assert remote.calls == ['get','copy','put']
    assert PRODUCTION_KEY in remote.objects


def test_real_insufficient_candidates_persists_failed_claim_to_r2(tmp_path,capsys):
    period = report_period(NOW)
    backend = DemoBackend(period,10)
    original = backend.respond
    def respond(request):
        if request.url.host == 'github.com':
            return httpx.Response(200,text='')
        if request.url.path == '/graphql':
            import json
            body = json.loads(request.content)
            return httpx.Response(200,json={'data':{alias:{'issueCount':0}
                                          for alias in body['variables']}})
        if request.url.path.endswith('/releases'):
            return httpx.Response(200,json=[])
        return original(request)
    backend.respond = respond
    remote = FakeS3(empty_remote(tmp_path))
    @contextmanager
    def factory(database):
        client,github,llm,sender = backend.clients()
        try:
            yield ReportPipeline(database,github,client,llm,sender,clock=lambda:NOW)
        finally:
            client.close()
    result = execute(tmp_path,remote,pipeline_factory=factory)
    assert result.status == 'APPLICATION_FAILED' and result.exit_code == 1
    assert result.pipeline_status == 'FAILED' and result.remote_updated
    assert remote.calls == ['get','copy','put']
    assert not backend.sent and backend.calls['api.openai.com/v1/responses'] == 0
    restored = tmp_path/'failed.db'
    restored.write_bytes(remote.objects[PRODUCTION_KEY])
    database = Database(restored)
    row = database.rows('SELECT mode,status,error,finished_at FROM automation_runs')[0]
    assert row['mode'] == 'live' and row['status'] == 'FAILED'
    assert row['error'].startswith('InsufficientCandidates:') and row['finished_at']
    assert not database.rows('SELECT * FROM report_history')
    assert not database.rows('SELECT * FROM watchlist')
    assert not database.rows('SELECT * FROM telegram_logs')
    import json
    summary = json.loads(database.rows('SELECT report FROM automation_runs')[0]['report'])['candidate_diagnostics']
    # The mock includes one mandatory Critical repository, still fewer than five.
    assert summary['funnel']['evidence supported'] == 1
    assert summary['funnel']['selectable'] == 1
    assert summary['exclusion_reasons']['insufficient_independent_evidence'] > 0
    assert summary['data_gaps']['previous_snapshot_missing'] > 0
    assert summary['top_candidates'] == []
    assert database.rows('SELECT * FROM weekly_snapshots')  # Existing failure observation policy.
    output = capsys.readouterr().out
    assert 'Candidate funnel:' in output
    assert 'Exclusion reasons:' in output and 'Data gaps' in output
    assert 'insufficient_independent_evidence' in output
    for secret in ('dummy-github','dummy-openai','dummy-telegram','dummy-chat','dummy-access','dummy-secret'):
        assert secret not in output
    database.close()


def test_integrity_failure_blocks_r2_upload(tmp_path,monkeypatch):
    import services.weekly_live_runner as runner
    original_validate = runner.validate_database
    checks = []
    def validate(path):
        checks.append(path)
        if len(checks) == 2:
            raise SQLiteBackupError('SQLite integrity check failed')
        return original_validate(path)
    monkeypatch.setattr(runner,'validate_database',validate)
    original = empty_remote(tmp_path)
    remote = FakeS3(original)
    result = execute(tmp_path,remote,pipeline_factory=terminal_factory())
    assert result.status == 'SQLITE_FINALIZE_FAILED' and result.exit_code == 4
    assert result.duplicate_delivery_risk
    assert remote.calls == ['get'] and remote.objects[PRODUCTION_KEY] == original


def test_pending_transaction_is_not_silently_committed(tmp_path):
    def leave_uncommitted(database):
        database.connection.execute("INSERT INTO repositories VALUES ('id:2',2,'pending','url','{}')")
    remote = FakeS3(empty_remote(tmp_path))
    result = execute(tmp_path,remote,pipeline_factory=terminal_factory(after_finish=leave_uncommitted))
    assert result.status == 'SQLITE_FINALIZE_FAILED'
    assert remote.calls == ['get']
    database = Database(tmp_path/'local'/'production.db')
    assert database.rows('SELECT * FROM repositories') == []
    database.close()


@pytest.mark.parametrize('operation',['copy','put'])
def test_upload_failure_is_not_success_even_after_pipeline_success(tmp_path,operation,capsys):
    original = empty_remote(tmp_path)
    remote = FakeS3(original,fail={operation:FakeClientError()})
    result = execute(tmp_path,remote,pipeline_factory=terminal_factory())
    assert result.status == 'R2_UPLOAD_FAILED' and result.exit_code == 5
    assert result.pipeline_status == 'SUCCESS' and not result.remote_updated
    assert result.duplicate_delivery_risk
    assert remote.objects[PRODUCTION_KEY] == original
    assert (tmp_path/'local'/'production.db').exists()
    if operation == 'put':
        assert any(key.startswith(BACKUP_PREFIX) for key in remote.objects)
    text = capsys.readouterr().out
    assert 'duplicate delivery possible' in text
    assert 'Sensitive response' not in text


@pytest.mark.parametrize('status,exit_code',[('PARTIAL_SUCCESS',2),('FAILED',1)])
def test_terminal_unsuccessful_pipeline_result_is_uploaded_without_promoting_status(tmp_path,status,exit_code):
    remote = FakeS3(empty_remote(tmp_path))
    result = execute(tmp_path,remote,pipeline_factory=terminal_factory(status))
    assert result.exit_code == exit_code and result.remote_updated
    assert result.pipeline_status == status
    assert result.status != 'SUCCESS'


def test_unexpected_exception_does_not_upload_even_if_pipeline_stored_failed(tmp_path,capsys):
    original = empty_remote(tmp_path)
    remote = FakeS3(original)
    result = execute(tmp_path,remote,pipeline_factory=terminal_factory('FAILED',RuntimeError('dummy-secret')))
    assert result.status == 'UNEXPECTED_FAILURE' and result.exit_code == 6
    assert remote.calls == ['get'] and remote.objects[PRODUCTION_KEY] == original
    assert (tmp_path/'local'/'production.db').exists()
    assert 'dummy-secret' not in capsys.readouterr().out


def test_unrecorded_terminal_failure_is_never_uploaded(tmp_path):
    from services.ranking_service import InsufficientCandidates
    remote = FakeS3(empty_remote(tmp_path))
    @contextmanager
    def factory(database):
        class Pipeline:
            def run(self,*args,**kwargs):
                raise InsufficientCandidates('no recorded claim')
        yield Pipeline()
    result = execute(tmp_path,remote,pipeline_factory=factory)
    assert result.status == 'RUN_STATE_INVALID' and result.exit_code == 4
    assert remote.calls == ['get']


def test_interrupt_does_not_run_final_upload(tmp_path):
    remote = FakeS3(empty_remote(tmp_path))
    result = execute(tmp_path,remote,pipeline_factory=terminal_factory('FAILED',KeyboardInterrupt()))
    assert result.status == 'INTERRUPTED' and result.exit_code == 130
    assert remote.calls == ['get']


def test_process_exit_has_no_finally_upload(tmp_path):
    remote = FakeS3(empty_remote(tmp_path))
    with pytest.raises(SystemExit):
        execute(tmp_path,remote,pipeline_factory=terminal_factory('FAILED',SystemExit(1)))
    assert remote.calls == ['get']


def test_invalid_r2_settings_block_restore_and_pipeline(tmp_path):
    configured = replace(live_settings(tmp_path/'production.db'),r2_secret_access_key='')
    remote = FakeS3()
    result = run_weekly_live_once(configured,clock=lambda:NOW,
        store=R2DatabaseStore(configured,remote),pipeline_factory=lambda _:pytest.fail('Must not run'))
    assert result.status == 'CONFIGURATION_FAILED' and result.exit_code == 3
    assert remote.calls == []


@pytest.mark.parametrize('flag',['--integration-test','--dry-run','--preview','--period-end','--mode','--force'])
def test_cli_rejects_nonproduction_modes_and_claim_bypass(flag):
    with pytest.raises(SystemExit) as raised:
        parser().parse_args([flag])
    assert raised.value.code == 2


def test_integration_db_is_blocked_before_production_pipeline(tmp_path):
    source = tmp_path/'remote.db'
    database = Database(source)
    database.start_run('start','end','integration-test')
    database.close()
    remote = FakeS3(source.read_bytes())
    result = execute(tmp_path,remote,pipeline_factory=lambda _:pytest.fail('Fixture entered production'))
    assert result.status == 'RESTORE_FAILED' and remote.calls == ['get']


def test_existing_local_database_is_preserved_without_r2_access(tmp_path):
    path = tmp_path/'local'/'production.db'
    database = Database(path)
    database.close()
    original = path.read_bytes()
    remote = FakeS3()
    result = execute(tmp_path,remote,pipeline_factory=lambda _:pytest.fail('Pipeline must not run'))
    assert result.status == 'RESTORE_FAILED' and remote.calls == []
    assert path.read_bytes() == original


@pytest.mark.parametrize('now,expected_end',[
    (datetime(2026,10,11,18,59,59,tzinfo=KST),'2026-10-04T19:00:00+09:00'),
    (datetime(2026,10,11,19,tzinfo=KST),'2026-10-11T19:00:00+09:00'),
    (datetime(2026,10,12,9,tzinfo=KST),'2026-10-11T19:00:00+09:00'),
])
def test_runner_reuses_exact_scheduler_period_even_when_delayed(tmp_path,now,expected_end):
    observed = []
    @contextmanager
    def factory(database):
        class Pipeline:
            def run(self,period,**kwargs):
                observed.append(period)
                run_id = database.claim_live_run(period.start.isoformat(),period.end.isoformat())
                database.finish_run(run_id,'SUCCESS')
                return {'mode':'live','status':'SUCCESS','run_id':run_id}
        yield Pipeline()
    configured = live_settings(tmp_path/'production.db')
    remote = FakeS3()
    result = run_weekly_live_once(configured,allow_initial_bootstrap=True,clock=lambda:now,
        store=R2DatabaseStore(configured,remote),pipeline_factory=factory,output=tmp_path/'output')
    assert result.status == 'SUCCESS'
    assert observed[0] == report_period(now)
    assert observed[0].end.isoformat() == expected_end
    assert observed[0].end-observed[0].start == timedelta(hours=168)
    assert observed[0].contains(observed[0].start)
    assert not observed[0].contains(observed[0].end)
