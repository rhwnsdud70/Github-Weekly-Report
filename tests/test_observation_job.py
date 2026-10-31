from dataclasses import replace
from datetime import timedelta
import socket

import httpx
import pytest

from config import observation_rules as rules
from services.observation_job import ObservationGitHub, run_boundary_observation, ObservationJobError
from services.observation_pool import build_pool
from services.observation_pool_validation import seal_pool, encode_pool
from services.r2_observation_store import R2ObservationStore, R2ObservationError
from tests.test_observation_pool import record
from tests.test_observation_validation import B, OBS_ID, payload
from tests.test_r2_observations import ObservationS3
from tests.test_r2_persistence import settings, FakeClientError, PRODUCTION_KEY


class VirtualClock:
    def __init__(self,seconds=60): self.wall = B+timedelta(seconds=seconds); self.tick = 0
    def now(self): return self.wall
    def monotonic(self): return self.tick
    def sleep(self,seconds): self.wall += timedelta(seconds=seconds); self.tick += seconds


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args,**kwargs): pytest.fail('Actual network forbidden')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(socket.socket,'connect',forbidden)


def setup_job(tmp_path, *, delay=60, count=1, failure=None, duration=0, page_failure=False, put_mode=None):
    clock = VirtualClock(delay)
    client = ObservationS3()
    client.put_mode = put_mode
    records = [replace(record(i),first_seen_at=B,last_seen_at=B,source_period_end=B,
                       watchlisted_at=B) for i in range(1,count+1)]
    pool = seal_pool(build_pool(records,as_of=B,fixture=True),pool_id=OBS_ID,
                     created_at=B,producer=payload().producer)
    key = 'test-observation-pools/v1/2026-11-01/'+OBS_ID+'.json'
    client.objects[key] = encode_pool(pool)
    client.modified[key] = B
    store = R2ObservationStore(settings(tmp_path/'not-created.db'),as_of=clock.now(),namespace='fixture',
                               profile='observation_job',client=client)
    calls = []
    def respond(request):
        calls.append(request)
        clock.sleep(duration)
        if request.url.host == 'github.com':
            return httpx.Response(200,text='invalid markup' if page_failure else
                                  '<h2><a href="/renamed/repository">renamed</a></h2>')
        identity = int(request.url.path.split('/')[-1])
        if failure and identity == 1:
            if failure == 'timeout': raise httpx.ReadTimeout('sensitive-key',request=request)
            return httpx.Response(failure,headers={'retry-after':'1'} if failure == 429 else {})
        return httpx.Response(200,headers={'x-ratelimit-remaining':'100','x-ratelimit-resource':'core'},
            json={'id':identity,'full_name':'renamed/repository' if identity == 1 else f'fixture/repo{identity}',
            'html_url':'https://github.com/renamed/repository' if identity == 1 else f'https://github.com/fixture/repo{identity}',
            'created_at':'2026-11-01T09:59:00Z','stargazers_count':12,'forks_count':0})
    http = httpx.Client(transport=httpx.MockTransport(respond))
    github = ObservationGitHub(http,clock=clock)
    return clock,client,store,github,calls


def run(bundle, **kw):
    clock,client,store,github,_ = bundle
    try:
        return run_boundary_observation(store=store,github=github,producer=payload().producer,
                                        clock=clock,observation_id=OBS_ID,**kw)
    finally:
        github.http.close()
        store.close()


@pytest.mark.parametrize('delay,coverage',[(60,'FULL'),(600,'FULL'),(3600,'FULL'),(14400,'NONE')])
def test_actual_times_and_independent_coverage(tmp_path,delay,coverage):
    bundle = setup_job(tmp_path,delay=delay)
    result = run(bundle)
    assert result.status == 'SUCCESS'
    assert result.envelope.payload.boundary_coverage == coverage
    assert result.envelope.payload.repositories[0].observed_at == B+timedelta(seconds=delay)
    assert result.receipt.publication_status == 'VERIFIED'
    assert result.envelope.payload.repositories[0].metadata.full_name == 'renamed/repository'
    assert result.envelope.payload.repositories[0].stars == 12
    assert result.envelope.payload.repositories[0].forks == 0
    assert result.boundary == B
    assert bundle[2].logical_requests == 7  # four LIST + pool GET + PUT + verification GET


def test_request_crossing_window_not_eligible(tmp_path):
    bundle = setup_job(tmp_path,delay=3540,duration=120)
    result = run(bundle)
    row = result.envelope.payload.repositories[0]
    assert row.request_started_at == B+timedelta(minutes=59)
    assert row.observed_at == B+timedelta(minutes=61)
    assert result.envelope.payload.boundary_coverage == 'NONE'
    assert result.status == 'SUCCESS'


def test_forty_metadata_no_search_graphql_and_no_operating_db(tmp_path):
    bundle = setup_job(tmp_path,count=40)
    result = run(bundle)
    assert result.status == 'SUCCESS'
    assert len(result.envelope.payload.repositories) == 40
    assert bundle[3].rest_attempts == 40
    assert bundle[3].trending_attempts == 1
    assert len(bundle[4]) == 41
    assert not (tmp_path/'not-created.db').exists()
    assert bundle[1].objects[PRODUCTION_KEY] == b'protected production bytes'
    assert bundle[1].objects['backups/protected.db'] == b'protected backup bytes'


@pytest.mark.parametrize('failure,category',[(404,'NOT_FOUND'),(403,'AUTHENTICATION_OR_PERMISSION'),
    (401,'AUTHENTICATION_OR_PERMISSION'),(429,'RATE_LIMIT'),('timeout','NETWORK_ERROR')])
def test_metadata_failures_partial_not_zero(tmp_path,failure,category):
    bundle = setup_job(tmp_path,count=2,failure=failure)
    result = run(bundle)
    assert result.status == 'PARTIAL_SUCCESS'
    row = result.envelope.payload.repositories[0]
    assert row.status == 'unavailable' and row.stars is None and row.forks is None
    assert row.diagnostics.category == category
    assert result.receipt.publication_status == 'VERIFIED'
    if failure in {401,403,429}:
        assert bundle[3].rest_attempts == 1
    if failure == 404:
        assert result.envelope.payload.repositories[1].status == 'confirmed'


def test_trending_parse_failure_preserves_metadata(tmp_path):
    result = run(setup_job(tmp_path,page_failure=True))
    assert result.status == 'PARTIAL_SUCCESS'
    assert result.envelope.payload.trending.status == 'unavailable'
    assert result.envelope.payload.repositories[0].status == 'confirmed'


@pytest.mark.parametrize('mode,status',[('commit-timeout','VERIFIED'),('drop','UNCONFIRMED')])
def test_publication_timeout(tmp_path,mode,status):
    result = run(setup_job(tmp_path,put_mode=mode))
    assert result.receipt.publication_status == status
    assert (result.status == 'SUCCESS') == (status == 'VERIFIED')


@pytest.mark.parametrize('kind',['absent','empty','corrupt','auth'])
def test_pool_failure_never_calls_github(tmp_path,kind):
    bundle = setup_job(tmp_path,count=0 if kind == 'empty' else 1)
    if kind == 'absent':
        bundle[1].objects = {k:v for k,v in bundle[1].objects.items() if not k.startswith('test-observation-pools/')}
    if kind == 'corrupt': bundle[1].corrupt = 'checksum'
    if kind == 'auth': bundle[1].fail['list'] = FakeClientError('AccessDenied',403)
    result = run(bundle)
    assert result.status == 'FAILED' and result.envelope is None
    assert bundle[4] == []
    assert 'put' not in bundle[1].calls


def test_deadline_preserves_partial_records(tmp_path):
    bundle = setup_job(tmp_path,count=40,duration=100)
    result = run(bundle)
    assert result.status == 'PARTIAL_SUCCESS'
    assert bundle[3].rest_attempts == 4
    assert sum(r.status == 'confirmed' for r in result.envelope.payload.repositories) == 4
    assert len(result.envelope.payload.repositories) == 40
    assert result.receipt.publication_status == 'VERIFIED'
    assert bundle[3].trending_attempts == 0


def test_numeric_adapter_physical_budget_includes_retries(tmp_path):
    bundle = setup_job(tmp_path,count=40)
    def transport(request): return httpx.Response(500)
    bundle[3].http.close()
    bundle[3].http = httpx.Client(transport=httpx.MockTransport(transport))
    result = run(bundle)
    assert result.status == 'FAILED'
    assert bundle[3].rest_attempts == rules.REST_ATTEMPT_LIMIT
    assert result.receipt.publication_status == 'VERIFIED'  # failed observation != failed publication


def test_reserve_stops_without_retry(tmp_path):
    bundle = setup_job(tmp_path,count=3)
    def transport(request):
        if request.url.host == 'github.com': return httpx.Response(200,text='unparseable')
        return httpx.Response(403,headers={'x-ratelimit-remaining':'0','x-ratelimit-resource':'core'})
    bundle[3].http.close()
    bundle[3].http = httpx.Client(transport=httpx.MockTransport(transport))
    result = run(bundle)
    assert bundle[3].rest_attempts == 1
    assert result.envelope.payload.repositories[0].diagnostics.category == 'RATE_LIMIT'


def test_job_reuse_store_rejected(tmp_path):
    bundle = setup_job(tmp_path)
    run(bundle)
    with pytest.raises(ObservationJobError,match='INVALID_STORE_CONTEXT'):
        run(bundle)


def test_cutoff_advances_without_budget_reset_and_cannot_move_boundary(tmp_path):
    bundle = setup_job(tmp_path)
    store = bundle[2]
    store.list_recent_pools()
    store.advance_cutoff(store.as_of+timedelta(minutes=1))
    assert store.logical_requests == 4
    with pytest.raises(R2ObservationError): store.advance_cutoff(B)
    with pytest.raises(R2ObservationError): store.advance_cutoff(B+timedelta(days=7))
    store.close(); bundle[3].http.close()


def test_offline_cli_default_and_mock_no_settings(tmp_path,monkeypatch,capsys):
    from scripts.run_boundary_observation_once import main
    from config.settings import Settings
    def forbidden(*a,**kw): pytest.fail('Credentials must not load')
    monkeypatch.setattr(Settings,'load',forbidden)
    assert main([]) == 0
    assert 'DISABLED' in capsys.readouterr().out
    assert main(['--mock']) == 0
    assert 'VERIFIED' in capsys.readouterr().out
    with pytest.raises(SystemExit): main(['--production'])


def test_duplicate_same_id_same_bytes_is_verified_without_overwrite(tmp_path):
    first = setup_job(tmp_path)
    result = run(first)
    key = result.receipt.object_key
    raw = first[1].objects[key]
    second = setup_job(tmp_path)
    second[1].objects[key] = raw
    second[1].modified[key] = first[0].now()
    repeated = run(second)
    assert repeated.receipt.publication_status == 'VERIFIED'
    assert second[1].objects[key] == raw


def test_duplicate_same_id_different_observation_conflicts(tmp_path):
    first = setup_job(tmp_path)
    result = run(first)
    key = result.receipt.object_key
    second = setup_job(tmp_path,delay=600)
    second[1].objects[key] = first[1].objects[key]
    second[1].modified[key] = first[0].now()
    repeated = run(second)
    assert repeated.status == 'FAILED'
    assert repeated.diagnostics == ('IMMUTABLE_OBJECT_CONFLICT',)
    assert second[1].objects[key] == first[1].objects[key]


def test_invalid_metadata_isolated_as_unavailable(tmp_path):
    bundle = setup_job(tmp_path,count=2)
    original = bundle[3].repository
    def repository(identity):
        data = original(identity)
        if identity == 1: data['stargazers_count'] = True
        return data
    bundle[3].repository = repository
    result = run(bundle)
    assert result.status == 'PARTIAL_SUCCESS'
    assert result.envelope.payload.repositories[0].stars is None
    assert result.envelope.payload.repositories[1].stars == 12


def test_invalid_trending_source_does_not_discard_good_metadata(tmp_path):
    bundle = setup_job(tmp_path)
    bundle[3].trending = lambda: ([('malformed/name/extra',1)],'a'*64)
    result = run(bundle)
    assert result.status == 'PARTIAL_SUCCESS'
    assert result.envelope.payload.trending.status == 'unavailable'


def test_trending_transport_retries_at_most_twice(tmp_path):
    bundle = setup_job(tmp_path)
    original = bundle[3].http
    def respond(request):
        if request.url.host == 'github.com': raise httpx.ReadTimeout('secret',request=request)
        return original.send(request)
    bundle[3].http = httpx.Client(transport=httpx.MockTransport(respond))
    result = run(bundle)
    original.close()
    assert result.status == 'PARTIAL_SUCCESS'
    assert bundle[3].trending_attempts == 2
    assert result.envelope.payload.trending.diagnostics.category == 'NETWORK_ERROR'


def test_sdk_request_guard_covers_retries_and_retains_budget(tmp_path):
    bundle = setup_job(tmp_path)
    store = bundle[2]
    seen = []
    store.set_request_guard(lambda:seen.append(True))
    store.list_recent_pools()
    assert len(seen) == 8  # logical gate and physical before-send for four calls
    assert store.logical_requests == 4
    with pytest.raises(R2ObservationError): store.set_request_guard(lambda:None)
    store.close(); bundle[3].http.close()


def test_reject_hidden_transport_retry_policy():
    http = httpx.Client(transport=httpx.HTTPTransport(retries=1))
    try:
        with pytest.raises(ObservationJobError,match='UNSUPPORTED_HTTP_RETRY_POLICY'):
            ObservationGitHub(http,clock=VirtualClock())
    finally: http.close()


def test_endpoint_allowlist_no_search_graphql_or_names(tmp_path):
    bundle = setup_job(tmp_path)
    from collectors.github_client import ApiBudgetExceeded
    for path in ('search/repositories','graphql','repos/owner/name','repositories/1/issues'):
        with pytest.raises(ApiBudgetExceeded):
            bundle[3].request('GET','https://api.github.com/'+path)
    assert bundle[4] == [] and bundle[3].rest_attempts == 0
    bundle[2].close(); bundle[3].http.close()


def test_wall_clock_jump_fails_closed_without_rewriting_times(tmp_path):
    bundle = setup_job(tmp_path)
    original = bundle[3].repository
    def repository(identity):
        data = original(identity)
        bundle[0].wall += timedelta(seconds=60)  # monotonic doesn't advance
        return data
    bundle[3].repository = repository
    result = run(bundle)
    assert result.status == 'FAILED'
    assert result.diagnostics == ('CLOCK_DURATION_MISMATCH',)
    assert 'put' not in bundle[1].calls


def test_deadline_during_pool_access_prevents_collection_and_upload(tmp_path):
    bundle = setup_job(tmp_path)
    original = bundle[1].list_objects_v2
    def listing(**kw):
        response = original(**kw)
        bundle[0].sleep(420)
        return response
    bundle[1].list_objects_v2 = listing
    result = run(bundle)
    assert result.status == 'FAILED'
    assert result.diagnostics == ('REQUEST_BUDGET_EXHAUSTED',)
    assert bundle[4] == [] and 'put' not in bundle[1].calls


def test_current_empty_pool_does_not_resurrect_old_pool(tmp_path):
    bundle = setup_job(tmp_path,count=0)
    old_time = B-timedelta(days=7)
    old_record = replace(record(1),first_seen_at=old_time,last_seen_at=old_time,
                         source_period_end=old_time,watchlisted_at=old_time)
    old = seal_pool(build_pool([old_record],as_of=old_time,fixture=True),pool_id=OBS_ID,
                    created_at=old_time,producer=payload().producer)
    key = 'test-observation-pools/v1/2026-10-25/'+OBS_ID+'.json'
    bundle[1].objects[key] = encode_pool(old)
    bundle[1].modified[key] = old_time
    result = run(bundle)
    assert result.diagnostics == ('POOL_EMPTY',)
    assert bundle[4] == []


def test_expired_pool_not_renewed(tmp_path):
    bundle = setup_job(tmp_path)
    old_time = B-timedelta(days=21)
    old_record = replace(record(1),first_seen_at=old_time,last_seen_at=old_time,
                         source_period_end=old_time)
    old = seal_pool(build_pool([old_record],as_of=old_time,fixture=True),pool_id=OBS_ID,
                    created_at=old_time,producer=payload().producer)
    bundle[1].objects = {k:v for k,v in bundle[1].objects.items() if not k.startswith('test-observation-pools/')}
    key = 'test-observation-pools/v1/2026-10-11/'+OBS_ID+'.json'
    bundle[1].objects[key] = encode_pool(old)
    bundle[1].modified[key] = old_time
    result = run(bundle)
    assert result.diagnostics == ('POOL_EMPTY',)
    assert bundle[4] == []


def test_fixture_pool_cannot_masquerade_as_production(tmp_path):
    bundle = setup_job(tmp_path)
    original_key = 'test-observation-pools/v1/2026-11-01/'+OBS_ID+'.json'
    key = 'observation-pools/v1/production/2026-11-01/'+OBS_ID+'.json'
    bundle[1].objects[key] = bundle[1].objects.pop(original_key)
    bundle[1].modified[key] = B
    bundle[2].close()
    bundle = (*bundle[:2],R2ObservationStore(settings(tmp_path/'not-created.db'),as_of=bundle[0].now(),
               namespace='production',profile='observation_job',client=bundle[1]),*bundle[3:])
    result = run(bundle)
    assert result.status == 'FAILED'
    assert result.diagnostics == ('FIXTURE_NAMESPACE_MISMATCH',)
    assert bundle[4] == [] and 'put' not in bundle[1].calls
