"""Phase5B component/regression wiring only; all endpoints are in-memory mocks."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from database.db import Database, DuplicateRunError
from models.observation import RepositoryMetadata, TrendingEntry
from models.serialization import dumps, report_from_dict
from services.candidate_diagnostics import format_observation_diagnostics
from services.demo import DemoBackend
from services.observation_consumer import consume_observations
from services.observation_pipeline import apply_observation, production_observation_loader
from services.observation_validation import seal_observation, encode_observation
from services.pipeline import ReportPipeline, write_output
from services.r2_observation_store import R2ObservationStore, R2ObservationError
from services.ranking_service import InsufficientCandidates
from services.score_service import star_score
from tests.test_observation_consumer import B, PERIOD, CUTOFF, observation
from tests.test_r2_observations import ObservationS3
from tests.test_r2_persistence import settings, FakeS3, PRODUCTION_KEY


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a,**kw): pytest.fail('Actual socket connection forbidden')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)


def envelope(backend, boundary, stars, *, index=1, forks=100, delay=60):
    p = observation(boundary,index=index,stars=stars,forks=forks,delay=delay)
    rows = []
    for raw in backend.repositories:
        meta = RepositoryMetadata(raw['id'],raw['full_name'],raw['html_url'],
            B-timedelta(days=100),description=raw['description'],topics=tuple(raw['topics']),language='Python')
        rows.append(replace(p.repositories[0],github_id=raw['id'],
            source_url=f'https://api.github.com/repositories/{raw["id"]}',metadata=meta))
    page = replace(p.trending,entries=tuple(TrendingEntry(r.metadata.full_name,i,r.github_id,'confirmed')
                                          for i,r in enumerate(rows,1)))
    return replace(p,fixture=False,mode='production-observation',repositories=tuple(rows),trending=page,
                   pool_ref=replace(p.pool_ref,key=p.pool_ref.key.replace('test-observation-pools/v1/',
                                                                         'observation-pools/v1/production/')))


def put(s3, p, modified=None):
    key = 'observations/v1/production/'+p.boundary_end.date().isoformat()+'/'+p.observation_id+'.json'
    s3.objects[key] = encode_observation(seal_observation(p))
    s3.modified[key] = modified or p.completed_at


@pytest.fixture
def make(tmp_path):
    resources = []
    def create(*, current=1200, previous=1000, latest=1500, failed_message=None, unavailable=False,
               empty=False, no_events=False):
        directory = tmp_path/str(len(resources)); directory.mkdir()
        db = Database(directory/'test.db')
        backend = DemoBackend(PERIOD,10,failed_message=failed_message)
        for raw in backend.repositories: raw['stargazers_count'] = latest; raw['forks_count'] = 150
        backend.seed_history(db)
        original = backend.respond
        if no_events:
            def respond(request):
                if request.url.path.endswith('/releases') or request.url.path == '/advisories':
                    return httpx.Response(200,json=[])
                return original(request)
            backend.respond = respond
        s3 = ObservationS3()
        if not empty:
            if current is not None: put(s3,envelope(backend,B,current))
            if previous is not None: put(s3,envelope(backend,PERIOD.start,previous,forks=80))
        calls = []
        def loader(period,cutoff,legacy):
            claims = db.rows("SELECT status FROM automation_runs WHERE mode='live'")
            assert claims == [{'status':'RUNNING'}], 'Consumer must run AFTER atomic Claim'
            calls.append((period,cutoff,legacy))
            if unavailable: raise R2ObservationError('list','NETWORK_ERROR')
            with R2ObservationStore(settings(directory/'unused.db'),as_of=cutoff,
                                   profile='report_read',client=s3) as store:
                return consume_observations(store=store,period=period,cutoff=cutoff,legacy_snapshots=legacy,
                                            prefer_object_snapshots=True)
        client,github,llm,sender = backend.clients()
        pipeline = ReportPipeline(db,github,client,llm,sender,clock=lambda:CUTOFF,observation_loader=loader)
        resources.append((db,client))
        return SimpleNamespace(db=db,backend=backend,s3=s3,calls=calls,pipeline=pipeline,path=directory,
            run=lambda: pipeline.run(PERIOD,mode='live',limit=10,output=directory/'report'))
    yield create
    for db,client in resources: client.close(); db.close()


def candidates(state):
    return json.loads((state.path/'report'/'candidates.json').read_text(encoding='utf-8'))['candidates']


def test_success_claim_then_consumer_then_boundary_scoring_latest_persistence(make):
    s = make()
    old = s.db.rows('SELECT * FROM weekly_snapshots')
    result = s.run()
    assert result['status'] == 'SUCCESS' and len(result['top']) == 5
    assert len(s.calls) == 1
    assert len(s.backend.deep_repositories) == 5
    assert s.backend.calls['api.openai.com/v1/responses'] == 10
    assert len(s.db.rows('SELECT * FROM report_history')) == 5
    assert len(s.db.rows('SELECT * FROM watchlist')) == 3
    assert {r['name'] for r in s.db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} == {
        'repositories','automation_runs','weekly_snapshots','weekly_metrics','report_history','watchlist','telegram_logs'}
    for c in candidates(s):
        assert c['repository']['stars'] == 1200 and c['latest_repository']['stars'] == 1500
        assert c['metrics']['star_delta']['value'] == 200
        assert c['metrics']['star_growth']['value'] == 20
        assert c['metrics']['fork_delta']['value'] == 20
        assert c['metrics']['fork_growth']['value'] == 25
        assert c['score']['components']['star_delta'] == star_score(1200,200)
        assert len(set(c['signals'])) == len(c['signals'])
        assert set(c['signals']) <= {'star','trending','activity','discussion','release','security','policy'}
    latest = s.db.rows('SELECT data FROM weekly_snapshots WHERE period_end=?',(PERIOD.end.isoformat(),))
    # Without live Trending at23:00, discovery finds8 of the10 observed IDs.
    # Observation inventory must NOT inject the remaining2 as new candidates.
    assert result['candidate_count'] == 8
    assert len(latest) == result['candidate_count']
    assert all(json.loads(r['data'])['stars'] == 1500 for r in latest)
    assert all(json.loads(r['data'])['measured_at'] == CUTOFF.isoformat() for r in latest)
    assert s.db.rows('SELECT * FROM weekly_snapshots WHERE period_end=?',(PERIOD.start.isoformat(),)) == old
    text = '\n'.join(item['text'] for item in s.backend.sent)
    assert '현재 Star: 1,500' in text and '평가 기준 Star: 1,200' in text
    assert '최신 Fork: 150' in text and '평가 기준 Fork: 100' in text
    assert '최근 7일 Star 증가: 200' in text
    assert 'Historical Trending: unavailable' not in text
    stored = json.loads(s.db.rows('SELECT report FROM automation_runs')[0]['report'])
    assert stored['observation_diagnostics'] == result['observation_diagnostics']
    assert result['observation_diagnostics']['star_comparison_confirmed'] == result['candidate_count']
    # New fields round-trip through artifact replay without any new sends/APIs.
    report = report_from_dict(json.loads((s.path/'report'/'report.json').read_text(encoding='utf-8')))
    before = len(s.backend.sent)
    write_output(report,s.path/'replay')
    assert len(s.backend.sent) == before
    assert (s.path/'replay'/'messages.txt').read_bytes() == (s.path/'report'/'messages.txt').read_bytes()


@pytest.mark.parametrize('state',['SUCCESS','FAILED','RUNNING','PARTIAL_SUCCESS'])
def test_existing_claim_never_calls_consumer_or_observation_r2(make,state):
    s = make()
    run_id = s.db.claim_live_run(PERIOD.start.isoformat(),PERIOD.end.isoformat())
    if state != 'RUNNING': s.db.finish_run(run_id,state)
    old = s.db.rows('SELECT * FROM automation_runs')
    with pytest.raises(DuplicateRunError): s.run()
    assert s.calls == [] and s.s3.calls == [] and not s.backend.calls
    assert s.db.rows('SELECT * FROM automation_runs') == old


@pytest.mark.parametrize('kwargs',[{'empty':True},{'unavailable':True},{'current':None},{'previous':None}])
def test_optional_missing_or_unavailable_does_not_abort_other_sources(make,kwargs):
    s = make(**kwargs)
    result = s.run()
    assert result['status'] == 'SUCCESS' and len(result['top']) == 5
    for c in candidates(s):
        assert c['metrics']['star_delta']['status'] == 'unknown'
        assert c['metrics']['star_growth']['status'] == 'unknown'
        assert c['metrics']['issues']['status'] == 'confirmed'
        assert c['latest_repository']['stars'] == 1500
    if kwargs.get('unavailable'):
        assert result['observation_diagnostics']['observation_unavailable'] == result['candidate_count']
        assert s.s3.calls == []


@pytest.mark.parametrize('previous,current,delta,growth_status',[(1300,1200,-100,'confirmed'),(0,1200,1200,'not_applicable')])
def test_negative_growth_and_previous_zero(make,previous,current,delta,growth_status):
    s = make(previous=previous,current=current)
    s.run()
    assert all(c['metrics']['star_delta']['value'] == delta for c in candidates(s))
    assert all(c['metrics']['star_growth']['status'] == growth_status for c in candidates(s))


def test_t_uses_boundary_size_band_not_latest(make):
    s = make(previous=1000,current=1200,latest=50000)
    s.run()
    c = candidates(s)[0]
    assert star_score(1200,200) != star_score(50000,200)
    assert c['score']['components']['star_delta'] == star_score(1200,200)
    assert c['latest_repository']['stars'] == 50000


def test_conflict_produces_unknown_not_max_or_fallback(make):
    s = make()
    put(s.s3,envelope(s.backend,B,1800,index=2))
    result = s.run()
    assert result['observation_diagnostics']['observation_conflicted'] == result['candidate_count']
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in candidates(s))
    assert all(c['repository']['stars'] is None for c in candidates(s))


def test_after_cutoff_publication_not_used(make):
    s = make()
    put(s.s3,envelope(s.backend,B,9000,index=2),modified=CUTOFF+timedelta(seconds=1))
    s.run()
    assert all(c['metrics']['star_delta']['value'] == 200 for c in candidates(s))


def test_trending_missing_retains_star_activity_release_sources(make):
    s = make()
    p = envelope(s.backend,B,1200)
    from models.observation import TrendingObservation, SafeDiagnostics
    page = TrendingObservation('unavailable',None,None,None,diagnostics=SafeDiagnostics(category='SOURCE_UNAVAILABLE'))
    put(s.s3,replace(p,trending=page,collection_status='PARTIAL_SUCCESS',boundary_coverage='PARTIAL'))
    result = s.run()
    assert result['observation_diagnostics']['trending_confirmed'] == 0
    assert all(c['metrics']['trending']['status'] == 'unknown' for c in candidates(s))
    assert all(c['metrics']['star_delta']['value'] == 200 for c in candidates(s))


def test_insufficient_evidence_keeps_failed_claim_latest_snapshots_and_no_send(make):
    s = make(empty=True,no_events=True)
    with pytest.raises(InsufficientCandidates) as raised: s.run()
    assert s.db.rows('SELECT status FROM automation_runs') == [{'status':'FAILED'}]
    report = json.loads(s.db.rows('SELECT report FROM automation_runs')[0]['report'])
    assert report['observation_diagnostics'] == raised.value.observation_diagnostics
    assert report['candidate_diagnostics']['funnel']['selectable'] < 5
    assert not s.backend.sent and not s.backend.calls['api.openai.com/v1/responses']
    assert s.db.rows('SELECT * FROM report_history') == []
    assert all(json.loads(r['data'])['stars'] == 1500 for r in s.db.rows(
        'SELECT data FROM weekly_snapshots WHERE period_end=?',(PERIOD.end.isoformat(),)))
    calls = list(s.s3.calls)
    with pytest.raises(DuplicateRunError): s.run()
    assert s.s3.calls == calls


def test_partial_telegram_success_preserved_without_duplicate_resend(make):
    s = make(failed_message='TOP 2')
    result = s.run()
    assert result['status'] == 'PARTIAL_SUCCESS'
    assert sum('TOP 2' in m['text'] for m in s.backend.sent) == 3
    before = list(s.backend.sent)
    with pytest.raises(DuplicateRunError): s.run()
    assert s.backend.sent == before


def test_wrong_id_context_not_matched_by_repository_name(make):
    s = make()
    p = envelope(s.backend,B,1200)
    rows = tuple(replace(r,github_id=r.github_id+100,source_url=f'https://api.github.com/repositories/{r.github_id+100}',
        metadata=replace(r.metadata,github_id=r.github_id+100)) for r in p.repositories)
    page = replace(p.trending,entries=tuple(replace(e,github_id=e.github_id+100) for e in p.trending.entries))
    put(s.s3,replace(p,repositories=rows,trending=page))
    s.run()
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in candidates(s))
    assert all(c['metrics']['trending']['status'] == 'unknown' for c in candidates(s))


def test_observation_alone_is_not_multiple_independent_sources(make,monkeypatch):
    from services.metric_service import derive_signals
    from services.validation_service import validate_candidate
    from models.domain import Candidate, Repository, Metric
    s = make()
    with R2ObservationStore(settings(s.path/'unused.db'),as_of=CUTOFF,profile='report_read',client=s.s3) as store:
        ctx = consume_observations(store=store,period=PERIOD,cutoff=CUTOFF)
    # Remove Trending page explicitly to test one Star fact and both growth forms.
    from models.observation_context import TrendingSelection
    rows = tuple(replace(r,trending_rank=Metric()) for r in ctx.repositories)
    ctx = replace(ctx,repositories=rows,trending=TrendingSelection('MISSING',B))
    raw = s.backend.repositories[0]
    c = Candidate(Repository(raw['id'],raw['full_name'],raw['html_url'],stars=1500))
    apply_observation(c,ctx,CUTOFF.isoformat())
    derive_signals(c)
    assert c.signals == {'star'}
    assert not validate_candidate(c,PERIOD)
    assert 'insufficient independent GitHub evidence' in c.validation_reasons


@pytest.mark.parametrize('mode',['dry-run','preview','integration-test'])
def test_nonlive_modes_never_use_injected_observation_loader(make,mode):
    s = make()
    if mode == 'preview': s.pipeline.sender = None
    kwargs = {}
    if mode == 'integration-test':
        from scripts.integration_test_fixture import ColdStartIntegrationFixture
        kwargs['integration_fixture'] = ColdStartIntegrationFixture()
    result = s.pipeline.run(PERIOD,mode=mode,limit=10,structure_only=mode != 'integration-test',
                            output=s.path/'dry',**kwargs)
    assert len(result['top']) == 5 and s.calls == [] and s.s3.calls == []


def test_unsafe_loader_exception_does_not_leak_secret(make,caplog):
    s = make()
    def fail(*a): raise RuntimeError('secret-do-not-print')
    s.pipeline.observation_loader = fail
    result = s.run()
    assert result['status'] == 'SUCCESS'
    assert 'secret-do-not-print' not in caplog.text
    assert 'secret-do-not-print' not in dumps(result)
    assert all(c['observation']['diagnostics'][0]['category'] == 'OBSERVATION_LOAD_FAILED' for c in candidates(s))


def test_claim_creation_failure_precedes_any_observation_or_api_work(make,monkeypatch):
    s = make()
    def fail(*a): raise RuntimeError('Claim unavailable')
    monkeypatch.setattr(s.db,'claim_live_run',fail)
    with pytest.raises(RuntimeError,match='Claim unavailable'): s.run()
    assert s.calls == [] and s.s3.calls == [] and not s.backend.calls


@pytest.mark.parametrize('kind',['authentication','permission','network','json','checksum','source'])
def test_r2_damage_diagnostics_preserved_without_failing_other_signals(make,kind):
    from tests.test_r2_persistence import FakeClientError
    s = make()
    if kind in {'authentication','permission'}:
        s.s3.fail['list'] = FakeClientError('AccessDenied',401 if kind == 'authentication' else 403)
    elif kind == 'network': s.s3.fail['list'] = OSError('secret must not escape')
    elif kind in {'json','checksum'}: s.s3.corrupt = kind
    else:
        p = envelope(s.backend,B,1200)
        put(s.s3,replace(p,repositories=(replace(p.repositories[0],source_url='https://untrusted.example/'),*p.repositories[1:])))
    result = s.run()
    assert result['status'] == 'SUCCESS'
    assert result['observation_diagnostics']['observation_unavailable'] == result['candidate_count']
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in candidates(s))
    assert all(c['metrics']['trending']['status'] == 'unknown' for c in candidates(s))
    assert 'secret must not escape' not in dumps(candidates(s))


def test_fixture_context_is_rejected_in_live_path(make):
    s = make()
    real = s.pipeline.observation_loader
    def fixture(*args): return replace(real(*args),namespace='fixture')
    s.pipeline.observation_loader = fixture
    result = s.run()
    assert result['status'] == 'SUCCESS'
    assert all(c['observation']['diagnostics'][0]['category'] == 'FIXTURE_NAMESPACE_MISMATCH' for c in candidates(s))
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in candidates(s))


def test_matching_legacy_and_observation_conflict_uses_consumer_policy(make):
    s = make()
    # Existing test-only legacy rows have truthful request time provenance here.
    with s.db.transaction():
        for raw in s.backend.repositories:
            identity = 'id:'+str(raw['id'])
            s.db.snapshot(identity,PERIOD.start.isoformat(),{'measured_at':(PERIOD.start+timedelta(seconds=60)).isoformat(),
                'request_started_at':(PERIOD.start+timedelta(seconds=59)).isoformat(),'stars':900,'forks':80})
    original = s.db.rows('SELECT * FROM weekly_snapshots')
    result = s.run()
    assert result['observation_diagnostics']['observation_conflicted'] == result['candidate_count']
    assert all(c['metrics']['star_delta']['status'] == 'unknown' for c in candidates(s))
    assert s.db.rows('SELECT * FROM weekly_snapshots WHERE period_end=?',(PERIOD.start.isoformat(),)) == original


def test_valid_legacy_previous_snapshot_used_without_mixing_latest(make):
    s = make(previous=None)
    with s.db.transaction():
        for raw in s.backend.repositories:
            s.db.snapshot('id:'+str(raw['id']),PERIOD.start.isoformat(),{
                'measured_at':(PERIOD.start+timedelta(seconds=60)).isoformat(),
                'request_started_at':(PERIOD.start+timedelta(seconds=59)).isoformat(),'stars':1000,'forks':80})
    result = s.run()
    assert result['observation_diagnostics']['legacy_snapshot_used'] == result['candidate_count']
    assert all(c['metrics']['star_delta']['value'] == 200 for c in candidates(s))


@pytest.mark.parametrize('upload_failure,failed_message,insufficient',[
    (False,None,False),(True,None,False),(False,'TOP 2',False),(False,None,True)])
def test_runner_observation_pipeline_upload_and_delivery_states(tmp_path,upload_failure,failed_message,insufficient):
    from services.weekly_live_runner import run_weekly_live_once
    from services.r2_persistence import R2DatabaseStore
    from tests.test_weekly_live_once import live_settings
    from tests.test_r2_persistence import FakeClientError
    backend = DemoBackend(PERIOD,10,failed_message=failed_message)
    if insufficient:
        original_respond = backend.respond
        def respond(request):
            if request.url.path.endswith('/releases') or request.url.path == '/advisories':
                return httpx.Response(200,json=[])
            return original_respond(request)
        backend.respond = respond
    source = tmp_path/'source.db'
    db = Database(source); backend.seed_history(db); db.close()
    original = source.read_bytes()
    remote = FakeS3(original,fail={'put':FakeClientError('AccessDenied',403)} if upload_failure else {})
    observed = ObservationS3()
    if not insufficient:
        put(observed,envelope(backend,B,1200)); put(observed,envelope(backend,PERIOD.start,1000,forks=80))
    config = live_settings(tmp_path/'local'/'production.db')
    @contextmanager
    def factory(database):
        client,github,llm,sender = backend.clients()
        def load(period,cutoff,legacy):
            assert database.rows("SELECT status FROM automation_runs WHERE mode='live'") == [{'status':'RUNNING'}]
            with R2ObservationStore(config,as_of=cutoff,profile='report_read',client=observed) as store:
                return consume_observations(store=store,period=period,cutoff=cutoff,legacy_snapshots=legacy,
                                            prefer_object_snapshots=True)
        try:
            yield ReportPipeline(database,github,client,llm,sender,clock=lambda:CUTOFF,observation_loader=load)
        finally: client.close()
    emitted = []
    result = run_weekly_live_once(config,clock=lambda:CUTOFF,store=R2DatabaseStore(config,remote),
        pipeline_factory=factory,output=tmp_path/'artifacts',emit=emitted.append)
    expected = 'FAILED' if insufficient else 'PARTIAL_SUCCESS' if failed_message else 'SUCCESS'
    local = Database(config.database_path)
    try:
        run = local.rows('SELECT * FROM automation_runs')[0]
        assert run['status'] == expected
        diagnostics = json.loads(run['report'])['observation_diagnostics']
        assert format_observation_diagnostics(diagnostics) in emitted
        assert len(local.rows('SELECT * FROM report_history')) == (0 if insufficient else 5)
        if insufficient:
            assert local.rows('SELECT * FROM telegram_logs') == []
            assert json.loads(run['report'])['candidate_diagnostics']['funnel']['selectable'] < 5
        else:
            assert len(local.rows('SELECT * FROM telegram_logs')) >= 7
    finally: local.close()
    assert bool(backend.sent) is not insufficient
    assert observed.calls
    if upload_failure:
        assert result.status == 'R2_UPLOAD_FAILED' and result.exit_code == 5
        assert result.duplicate_delivery_risk
        assert remote.objects[PRODUCTION_KEY] == original
    else:
        assert result.status == ('APPLICATION_FAILED' if insufficient else expected) and result.remote_updated
        assert remote.objects[PRODUCTION_KEY] != original


def test_default_production_factory_wires_lazy_observation_loader_without_requests(tmp_path):
    from services.weekly_live_runner import production_pipeline
    from tests.test_weekly_live_once import live_settings
    config = live_settings(tmp_path/'production.db')
    db = Database(':memory:')
    try:
        with production_pipeline(config,db,lambda:CUTOFF) as pipeline:
            assert callable(pipeline.observation_loader)
            assert db.rows('SELECT * FROM automation_runs') == []
    finally: db.close()


def test_boundary_objects_preferred_over_earlier_nonconflicting_legacy(make):
    s = make()
    with s.db.transaction():
        for raw in s.backend.repositories:
            s.db.snapshot('id:'+str(raw['id']),PERIOD.start.isoformat(),{
                'measured_at':(PERIOD.start+timedelta(seconds=20)).isoformat(),
                'request_started_at':(PERIOD.start+timedelta(seconds=19)).isoformat(),'stars':900,'forks':80})
    result = s.run()
    assert result['observation_diagnostics']['legacy_snapshot_used'] == 0
    assert all(c['metrics']['star_delta']['value'] == 200 for c in candidates(s))


def test_all_telegram_failures_keep_original_failed_status_and_attempt_limits(make):
    s = make()
    def fail(request):
        s.backend.sent.append(json.loads(request.content))
        return httpx.Response(503,json={'ok':False,'error_code':503})
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        s.pipeline.sender.client = client
        result = s.run()
    assert result['status'] == 'FAILED'
    assert all(r['attempts'] == 3 and r['status'] == 'FAILED' for r in result['transmissions'])
    assert len(s.db.rows('SELECT * FROM telegram_logs')) == 3*result['message_count']
    sent = len(s.backend.sent)
    with pytest.raises(DuplicateRunError): s.run()
    assert len(s.backend.sent) == sent


def test_previous_report_history_preserved(make):
    s = make()
    identity = 'id:'+str(s.backend.repositories[0]['id'])
    with s.db.transaction():
        s.db.history(identity,PERIOD.start.isoformat(),2,42,{'event_ids':[],'narrative':{'project':['old report']}})
    old = s.db.rows('SELECT * FROM report_history WHERE period_end=?',(PERIOD.start.isoformat(),))
    s.run()
    assert s.db.rows('SELECT * FROM report_history WHERE period_end=?',(PERIOD.start.isoformat(),)) == old


def test_discovery_priority_keeps_latest_stars_while_t_uses_boundary():
    from models.domain import Candidate, Repository, Metric
    from services.metric_service import calculate_metrics
    from services.score_service import score_candidate
    repo = Repository(1,'synthetic/new','https://github.com/synthetic/new',stars=100,
                      created_at=(B-timedelta(days=10)).isoformat())
    c = Candidate(repo,latest_repository=replace(repo,stars=1500),observation={'enabled':True})
    c.metrics.update({'star_delta':Metric.known(200),'star_growth':Metric.known(20)})
    calculate_metrics(c,PERIOD)
    assert c.metric('new_repository_priority').value is True
    assert score_candidate(c).components['star_delta'] == star_score(100,200)
    assert star_score(100,200) != star_score(1500,200)


def test_consumer_default_earliest_policy_remains_compatible(make):
    from models.observation_context import LegacySnapshot
    s = make()
    previous = PERIOD.start
    legacy = (LegacySnapshot(900001,previous,previous+timedelta(seconds=20),900,80,
                             request_started_at=previous+timedelta(seconds=19)),)
    values = []
    for preferred in (False,True):
        with R2ObservationStore(settings(s.path/'unused.db'),as_of=CUTOFF,profile='report_read',client=s.s3) as store:
            ctx = consume_observations(store=store,period=PERIOD,cutoff=CUTOFF,legacy_snapshots=legacy,
                                       prefer_object_snapshots=preferred)
            values.append(ctx.for_repository(900001).comparison.star_delta.value)
    assert values == [300,200]
