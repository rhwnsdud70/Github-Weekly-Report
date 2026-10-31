"""Phase6: Producer-generated fixture bytes through the entire claimed batch."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import socket

import httpx
import pytest

from config import rules
from config.settings import Settings
from database.db import Database
from database.sqlite_backup import validate_database, SQLiteBackupError
from models.serialization import dumps, report_from_dict
from services.observation_consumer import ObservationConsumerError
from services.observation_pipeline import validate_pipeline_context
from services.observation_pool import PoolValidationError
from services.r2_observation_store import R2ObservationStore
from services.observation_validation import canonical_payload
from services.r2_persistence import PRODUCTION_KEY
from services.score_service import star_score
from telegram.splitter import units, split_messages
from telegram.formatter import format_report
from tests.observation_e2e_support import ObservationE2E, SYNTHETIC_FIXTURE
from tests.test_observation_consumer import B, PERIOD, CUTOFF
from tests.test_r2_persistence import FakeClientError


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args,**kwargs): pytest.fail('External client/network/credential loading forbidden')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(httpx.HTTPTransport,'__init__',forbidden)
    monkeypatch.setattr(Settings,'load',forbidden)
    monkeypatch.setattr('services.r2_persistence.create_r2_client',forbidden)
    monkeypatch.setattr('services.r2_observation_store.create_r2_client',forbidden)


@pytest.fixture
def harness(tmp_path):
    assert SYNTHETIC_FIXTURE
    return ObservationE2E(tmp_path)


def test_two_boundary_producer_consumer_report_database_and_upload(harness):
    h = harness
    previous = {r['id']:1000 for r in h.backend.repositories}
    current = {r['id']:1200 for r in h.backend.repositories}
    previous[900002],current[900002] = 2000,2300
    h.both(previous=previous,current=current)
    protected = h.observation_bytes()
    assert h.remote.objects[PRODUCTION_KEY] == h.initial
    assert len(h.jobs) == 2 and all(j.receipt.publication_status == 'VERIFIED' for j in h.jobs)
    assert all(j.envelope.payload.fixture for j in h.jobs)
    # Delete every producer result reference from the harness before consumption.
    # Consumers can only reconstruct state via the shared FakeS3 object's bytes.
    job_receipts = [j.receipt for j in h.jobs]
    h.jobs.clear()
    result = h.run()
    assert result.status == 'SUCCESS' and result.remote_updated
    assert h.consumer_calls == 1 and len(h.contexts) == 1
    context = h.contexts[0]
    assert context.namespace == 'fixture'
    row = context.for_repository(900001)
    assert row.comparison.star_delta.value == 200 and row.comparison.star_growth.value == 20
    assert row.comparison.fork_delta.value == 50 and row.comparison.fork_growth.value == 50
    assert context.for_repository(900002).comparison.star_delta.value == 300
    assert context.for_repository(900002).comparison.star_growth.value == 15
    assert row.trending_rank.value == 1
    assert row.current.snapshot.provenance.observed_at == B+timedelta(seconds=60.1)
    assert row.previous.snapshot.provenance.observed_at == PERIOD.start+timedelta(seconds=60.1)
    assert row.current.snapshot.provenance.sha256 == job_receipts[1].sha256
    assert row.current.snapshot.provenance.object_key == job_receipts[1].object_key
    assert h.observation_bytes() == protected  # Database upload cannot touch any observation.
    assert h.remote.calls == ['get','copy','put']
    assert any(v == h.initial for k,v in h.remote.objects.items() if k.startswith('backups/'))
    assert all(key.startswith(('test-observations/','test-observation-pools/')) for key in protected)
    assert not any(k.startswith(('observations/v1/production/','observation-pools/v1/production/')) for k in h.remote.objects)
    for receipt in job_receipts:
        raw = json.loads(protected[receipt.object_key])
        assert raw['payload']['fixture'] is True
        assert raw['integrity']['payload_sha256'] == receipt.sha256
        assert hashlib.sha256(canonical_payload(raw['payload'])).hexdigest() == receipt.sha256
    # The real strict production context validator remains incompatible with fixtures.
    with pytest.raises(ObservationConsumerError,match='FIXTURE_NAMESPACE_MISMATCH'):
        validate_pipeline_context(context,PERIOD,CUTOFF)
    cs = h.candidates()
    a = next(c for c in cs if c['repository']['github_id'] == 900001)
    assert a['repository']['stars'] == 1200 and a['latest_repository']['stars'] == 1500
    assert a['repository']['forks'] == 150 and a['latest_repository']['forks'] == 200
    assert a['score']['components']['star_delta'] == star_score(1200,200)
    b = next(c for c in cs if c['repository']['github_id'] == 900002)
    assert b['score']['components']['star_delta'] == star_score(2300,300)
    assert star_score(2300,300) != star_score(1500,300)
    assert a['observation']['namespace'] == 'fixture'
    assert set(a['signals']) <= {'star','trending','activity','discussion','release','security','policy'}
    assert len(set(a['signals'])) == len(a['signals'])
    assert all(len(c['signals']) >= rules.MIN_SIGNALS or any(e['kind'] == 'critical' for e in c['events']) for c in cs)
    text = '\n'.join(m['text'] for m in h.backend.sent)
    assert '현재 Star: 1,500' in text and '평가 기준 Star: 1,200' in text
    assert '최신 Fork: 200' in text and '평가 기준 Fork: 150' in text
    assert '2026-11-01T10:01:00.100000+00:00' in text
    assert all(units(m['text']) <= rules.TELEGRAM_LIMIT for m in h.backend.sent)
    assert len(h.backend.deep_repositories) == 5
    assert h.backend.calls['api.openai.com/v1/responses'] == 10
    db = h.remote_database()
    try:
        assert len(db.rows('SELECT * FROM report_history')) == 5
        assert len(db.rows('SELECT * FROM watchlist')) == 3
        assert db.rows('SELECT status FROM automation_runs') == [{'status':'SUCCESS'}]
        latest = [json.loads(r['data']) for r in db.rows('SELECT data FROM weekly_snapshots')]
        assert latest and all(r['stars'] == 1500 and r['forks'] == 200 for r in latest)
        assert all(r['measured_at'] == CUTOFF.isoformat() for r in latest)
        assert {r['name'] for r in db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} == {
            'repositories','weekly_snapshots','weekly_metrics','report_history','watchlist','telegram_logs','automation_runs'}
        saved = json.loads(db.rows('SELECT report FROM automation_runs')[0]['report'])
        assert len(saved['top']) == 5 and len(set(saved['top'])) == 5
        assert h.backend.repositories[-1]['full_name'] in saved['top']  # Actual Critical exception.
        assert db.rows('SELECT * FROM telegram_logs')
        assert db.rows('PRAGMA journal_mode') == [{'journal_mode':'delete'}]
    finally: db.close()
    validate_database(h.root/'inspect.db')
    assert all(c.is_closed for c in h.http_clients)
    # API composition starts only after DB restore; observer consumption follows claim.
    report_start = next(i for i,e in enumerate(h.trace) if e[0] == 'database' and e[1] == 'get')
    consumer = next(i for i,e in enumerate(h.trace) if e[:2] == ('consumer','after-claim'))
    assert consumer > report_start


@pytest.mark.parametrize('previous,current,delta,growth_status',[
    (1000,1200,200,'confirmed'),(1300,1200,-100,'confirmed'),(0,1200,1200,'not_applicable'),
    (None,1200,None,'unknown'),(1000,None,None,'unknown'),(None,None,None,'unknown')])
def test_boundary_cases_through_producer_and_runner(harness,previous,current,delta,growth_status):
    h = harness.both(previous=previous,current=current)
    assert h.run().status == 'SUCCESS'  # Other independent actual synthetic events remain.
    for c in h.candidates():
        assert c['metrics']['star_delta']['value'] == delta
        assert c['metrics']['star_growth']['status'] == growth_status
        assert c['latest_repository']['stars'] == 1500
    db = h.remote_database()
    try:
        assert all(json.loads(r['data'])['stars'] == 1500 for r in db.rows('SELECT data FROM weekly_snapshots'))
    finally: db.close()


def test_trending_failed_producer_does_not_discard_metadata(harness):
    h = harness.both(fault='trending')
    assert h.jobs[-1].status == 'PARTIAL_SUCCESS'
    assert h.jobs[-1].receipt.publication_status == 'VERIFIED'
    assert h.run().status == 'SUCCESS'
    assert all(c['metrics']['trending']['status'] == 'unknown' for c in h.candidates())
    assert all(c['metrics']['star_delta']['value'] == 200 for c in h.candidates())


def test_concurrent_producers_same_timestamp_conflict(harness):
    h = harness.both()
    h.produce(B,stars=1800,sequence=2)  # Independent synthetic concurrent Job clock.
    assert h.run().status == 'SUCCESS'
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in h.candidates())
    assert h.contexts[0].for_repository(900001).current.status == 'CONFLICT'


@pytest.mark.parametrize('kind,category',[
    ('json','INVALID_JSON'),('checksum','CHECKSUM_MISMATCH'),
    ('permission','AUTHENTICATION_OR_PERMISSION'),('network','NETWORK_ERROR'),
    ('pagination','INCOMPLETE_LIST')])
def test_consumer_failure_after_real_producer_publication(harness,kind,category):
    h = harness.both()
    if kind in {'json','checksum'}: h.observation.corrupt = kind
    elif kind == 'permission': h.observation.fail['list'] = FakeClientError('AccessDenied',403)
    elif kind == 'network': h.observation.fail['list'] = OSError('sensitive must not be exposed')
    else: h.observation.truncated = True
    assert h.run().status == 'SUCCESS'
    assert category in {d.category for d in h.contexts[0].diagnostics}
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in h.candidates())
    assert all(c['metrics']['trending']['status'] == 'unknown' for c in h.candidates())
    assert 'sensitive must not be exposed' not in dumps(h.candidates())


@pytest.mark.parametrize('fault',['pool','metadata','one-metadata','put','commit-timeout'])
def test_producer_failures_keep_optional_weekly_path_safe(harness,fault):
    h = harness
    h.produce(PERIOD.start,stars=1000,forks=100)
    if fault == 'pool':
        before = h.observation_bytes()
        with pytest.raises(PoolValidationError) as raised: h.produce(B,fault=fault)
        assert raised.value.category == 'FUTURE_DATA'
        assert h.observation_bytes() == before
    else:
        job = h.produce(B,fault=fault)
        if fault == 'put':
            assert job.receipt.publication_status == 'UNCONFIRMED'
            assert job.receipt.error_category == 'NETWORK_ERROR'
        elif fault == 'commit-timeout': assert job.receipt.publication_status == 'VERIFIED'
        elif fault == 'one-metadata': assert job.status == 'PARTIAL_SUCCESS'
        else: assert job.envelope.payload.repositories[0].status == 'unavailable'
    assert h.run().status == 'SUCCESS'
    if fault in {'pool','metadata','put'}:
        a = next(c for c in h.candidates() if c['repository']['github_id'] == 900001)
        assert a['metrics']['star_delta']['status'] == 'unknown'


def test_late_visibility_after_fixed_cutoff_is_not_selected(harness):
    h = harness
    h.produce(PERIOD.start,stars=1000,forks=100)
    job = h.produce(B,slow_publication=4*3600)
    assert job.receipt.publication_status == 'UNCONFIRMED'
    # Real synthetic observed_at remains near19:01, but publication completes
    # after the reader's23:00 fixed cutoff. This is not a late-metadata rewrite.
    assert job.envelope.payload.repositories[0].observed_at == B+timedelta(seconds=60.1)
    assert h.observation.modified[job.receipt.object_key] > CUTOFF
    assert h.run().status == 'SUCCESS'
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in h.candidates())


@pytest.mark.parametrize('failed_message,expected',[('TOP 2','PARTIAL_SUCCESS'),(None,'SUCCESS')])
def test_report_replay_duplicate_claim_and_split_order(harness,failed_message,expected):
    h = harness.both()
    h.backend.failed_message = failed_message
    assert h.run().status == expected
    rows = h.candidates()
    report_path = next((h.root/'artifacts').rglob('report.json'))
    report = report_from_dict(json.loads(report_path.read_text(encoding='utf-8')))
    # Force long section-safe messages using validated existing paragraphs only.
    report.top[0].narrative['project'] = report.top[0].narrative['project']*80
    messages = split_messages(format_report(report))
    assert all(units(m.text) <= rules.TELEGRAM_LIMIT for m in messages)
    ranks = [m.rank for m in messages if m.kind == 'top']
    assert ranks == sorted(ranks) and ranks.count(1) > 1
    before = (list(h.observation.calls),dict(h.backend.calls),len(h.backend.sent))
    old_remote = h.remote.objects[PRODUCTION_KEY]
    assert h.replay().status == 'SKIPPED_ALREADY_CLAIMED'
    assert (list(h.observation.calls),dict(h.backend.calls),len(h.backend.sent)) == before
    assert h.remote.objects[PRODUCTION_KEY] == old_remote


def test_evidence_insufficient_failed_claim_persisted_and_replay_blocked(tmp_path):
    h = ObservationE2E(tmp_path,no_events=True)
    # Producer metadata is available but Trending collection fails and growth
    # is absent: the file/metadata itself is not an independent evidence signal.
    h.both(previous=1200,current=1200,fault='trending')
    result = h.run()
    assert result.status == 'APPLICATION_FAILED' and result.remote_updated
    assert not h.backend.sent and not h.backend.calls['api.openai.com/v1/responses']
    db = h.remote_database()
    try:
        assert db.rows('SELECT status FROM automation_runs') == [{'status':'FAILED'}]
        assert db.rows('SELECT * FROM report_history') == []
        assert db.rows('SELECT * FROM weekly_snapshots')
        report = json.loads(db.rows('SELECT report FROM automation_runs')[0]['report'])
        assert report['candidate_diagnostics']['funnel']['selectable'] < 5
    finally: db.close()
    before = list(h.observation.calls),dict(h.backend.calls)
    assert h.replay().status == 'SKIPPED_ALREADY_CLAIMED'
    assert (list(h.observation.calls),dict(h.backend.calls)) == before


@pytest.mark.parametrize('stage',['restore','corrupt-restore','pipeline','evidence','report','finalize','integrity','backup','upload','claim'])
def test_failure_boundaries_preserve_remote_database_and_claims(harness,monkeypatch,stage):
    h = harness.both()
    def fail(*a,**kw): raise RuntimeError('synthetic failure; never expose raw response')
    if stage == 'restore': h.remote.fail['get'] = OSError('sensitive endpoint')
    elif stage == 'corrupt-restore': h.remote.objects[PRODUCTION_KEY] = b'corrupted synthetic SQLite'
    elif stage == 'pipeline': monkeypatch.setattr('services.candidate_service.CandidateCollector.collect',fail)
    elif stage == 'evidence': monkeypatch.setattr('services.pipeline.validate_candidate',fail)
    elif stage == 'report': monkeypatch.setattr('services.pipeline.write_output',fail)
    elif stage == 'finalize': monkeypatch.setattr('services.weekly_live_runner.checkpoint_and_close',fail)
    elif stage == 'integrity':
        from services import weekly_live_runner
        original = weekly_live_runner.validate_database
        checks = []
        def validation(path):
            checks.append(path)
            if len(checks) == 2: raise SQLiteBackupError('Synthetic post-finalize integrity failure')
            return original(path)
        monkeypatch.setattr(weekly_live_runner,'validate_database',validation)
    elif stage == 'upload': h.remote.fail['put'] = FakeClientError('AccessDenied',403)
    elif stage == 'backup': h.remote.fail['copy'] = FakeClientError('AccessDenied',403)
    elif stage == 'claim': monkeypatch.setattr(Database,'claim_live_run',fail)
    original_remote = h.remote.objects[PRODUCTION_KEY]
    expected = {'restore':'RESTORE_FAILED','corrupt-restore':'RESTORE_FAILED',
                'finalize':'SQLITE_FINALIZE_FAILED','integrity':'SQLITE_FINALIZE_FAILED',
                'backup':'R2_UPLOAD_FAILED','upload':'R2_UPLOAD_FAILED'}
    observed_before = list(h.observation.calls)
    result = h.run()
    assert result.status == expected.get(stage,'UNEXPECTED_FAILURE')
    assert h.remote.objects[PRODUCTION_KEY] == original_remote
    if stage in {'restore','corrupt-restore','claim'}:
        assert h.consumer_calls == 0
        assert h.observation.calls == observed_before
        assert not h.backend.sent and not h.backend.calls
    elif stage == 'pipeline':
        assert h.consumer_calls == 0
        assert h.observation.calls == observed_before
    else: assert h.consumer_calls == 1
    if stage not in {'finalize','integrity','backup','upload'}: assert not h.backend.sent
    if stage not in {'restore','corrupt-restore'}:
        validate_database(h.config.database_path)
        db = Database(h.config.database_path)
        try:
            statuses = db.rows('SELECT status FROM automation_runs')
            assert statuses == ([] if stage == 'claim' else [{'status':'SUCCESS' if stage in {'finalize','integrity','backup','upload'} else 'FAILED'}])
        finally: db.close()
    assert 'synthetic failure; never expose raw response' not in '\n'.join(h.output)
    if stage in {'finalize','integrity','backup','upload'}: assert result.duplicate_delivery_risk
    if stage == 'upload': assert any(v == h.initial for k,v in h.remote.objects.items() if k.startswith('backups/'))
    if stage in {'finalize','integrity'}: assert h.remote.calls == ['get']


@pytest.mark.parametrize('status',['SUCCESS','FAILED','RUNNING','PARTIAL_SUCCESS'])
def test_existing_remote_claim_prevents_all_report_clients_and_observation_reads(harness,status):
    h = harness.both()
    source = h.root/'claimed.db'
    db = Database(source)
    run_id = db.claim_live_run(PERIOD.start.isoformat(),PERIOD.end.isoformat())
    if status != 'RUNNING': db.finish_run(run_id,status)
    db.close()
    h.remote.objects[PRODUCTION_KEY] = source.read_bytes()
    old = h.remote.objects[PRODUCTION_KEY]
    observation_calls = list(h.observation.calls)
    assert h.run().status == 'SKIPPED_ALREADY_CLAIMED'
    assert h.consumer_calls == 0 and not h.http_clients and not h.backend.calls
    assert h.observation.calls == observation_calls
    assert h.remote.objects[PRODUCTION_KEY] == old


def test_same_published_object_can_be_read_twice_without_new_signals_or_writes(harness):
    h = harness.both()
    receipt = h.jobs[-1].receipt
    original = h.observation_bytes()
    calls = list(h.observation.calls)
    with R2ObservationStore(h.config,as_of=CUTOFF,namespace='fixture',profile='report_read',client=h.observation) as store:
        first = store.get_observation(B,receipt.object_id)
        second = store.get_observation(B,receipt.object_id)
    assert first == second
    assert first.validation.envelope.integrity.payload_sha256 == receipt.sha256
    assert h.observation_bytes() == original
    assert h.observation.calls == calls+['get','get']
    assert h.run().status == 'SUCCESS'
    assert all(len(c['signals']) == len(set(c['signals'])) for c in h.candidates())


def test_late_job_records_none_coverage_without_inventing_boundary_values(harness):
    h = harness.both(delay=4*3600)
    assert h.jobs[-1].envelope.payload.collection_status == 'SUCCESS'
    assert h.jobs[-1].envelope.payload.boundary_coverage == 'NONE'
    h.clock.wall = CUTOFF+timedelta(minutes=10)
    assert h.run().status == 'SUCCESS'
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in h.candidates())
    assert 'SOURCE_INELIGIBLE' in {d.category for d in h.contexts[0].diagnostics}


def test_star_delta_growth_and_duplicate_pages_still_one_star_fact(tmp_path):
    h = ObservationE2E(tmp_path,no_events=True)
    h.both(fault='trending')
    h.produce(B,sequence=2,fault='trending')  # duplicate metadata, no extra evidence.
    original = h.backend.respond
    def respond(request):
        if request.url.path == '/graphql':
            body = json.loads(request.content)
            if 'createdAt' in body['query']:
                return httpx.Response(200,json={'data':{
                    alias:{'issueCount':0,'nodes':[],'pageInfo':{'hasNextPage':False}}
                    for alias in body['variables']}})
        return original(request)
    h.backend.respond = respond
    assert h.run().status == 'APPLICATION_FAILED'
    assert all(c['signals'] == ['star'] for c in h.candidates())
    assert not h.backend.sent and not h.backend.calls['api.openai.com/v1/responses']


def test_all_telegram_failures_are_recorded_and_failed_claim_blocks_replay(harness):
    h = harness.both()
    original = h.backend.respond
    def respond(request):
        if request.url.host == 'api.telegram.org':
            h.backend.sent.append(json.loads(request.content))
            return httpx.Response(503,json={'ok':False,'error_code':503})
        return original(request)
    h.backend.respond = respond
    assert h.run().status == 'APPLICATION_FAILED'
    db = h.remote_database()
    try:
        run = db.rows('SELECT status,report FROM automation_runs')[0]
        assert run['status'] == 'FAILED'
        report = json.loads(run['report'])
        assert all(r['attempts'] == 3 and r['status'] == 'FAILED' for r in report['transmissions'])
        assert len(db.rows('SELECT * FROM telegram_logs')) == 3*report['message_count']
    finally: db.close()
    old = len(h.backend.sent),list(h.observation.calls)
    assert h.replay().status == 'SKIPPED_ALREADY_CLAIMED'
    assert (len(h.backend.sent),list(h.observation.calls)) == old


def test_default_production_validator_still_rejects_published_fixture_objects(tmp_path):
    h = ObservationE2E(tmp_path,strict_context=True).both()
    assert h.run().status == 'SUCCESS'  # Release/activity can still support TOP5.
    for c in h.candidates():
        assert c['observation']['diagnostics'][0]['category'] == 'FIXTURE_NAMESPACE_MISMATCH'
        assert c['metrics']['star_delta']['status'] == 'unknown'
        assert c['metrics']['trending']['status'] == 'unknown'


def test_producer_namespace_enforcement_cannot_write_fixture_into_production(harness):
    h = harness.both()
    original = h.observation_bytes()
    calls = list(h.observation.calls)
    from services.observation_validation import ObservationValidationError
    with R2ObservationStore(h.config,as_of=CUTOFF,client=h.observation) as store:
        with pytest.raises(ObservationValidationError,match='FIXTURE_NAMESPACE_MISMATCH'):
            store.publish_observation(h.jobs[-1].envelope)
    assert h.observation_bytes() == original
    assert h.observation.calls == calls
    assert not any(k.startswith('observations/v1/production/') for k in h.remote.objects)
