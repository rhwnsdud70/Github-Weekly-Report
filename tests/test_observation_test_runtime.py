"""Manual runtime E2E: real components, Synthetic HTTP and SDK bytes only."""
from datetime import timedelta
from io import BytesIO
from pathlib import Path
import socket
import sqlite3
from unittest.mock import patch

import httpx
import pytest
import yaml

from models.observation import Producer
from services import observation_test_runtime as runtime
from services.observation_consumer import consume_observations
from services.r2_observation_store import R2ObservationStore, R2ObservationError
from services.observation_validation import validate_observation, ObservationValidationError
from services.scheduler import report_period
from scripts.run_observation_test_once import main
from tests.test_r2_observations import ObservationS3
from tests.test_r2_persistence import FakeClientError
from tests.test_observation_validation import B


ROOT = Path(__file__).resolve().parents[1]
ENV = {'OBS_R2_ENDPOINT': 'https://'+'a'*32+'.r2.cloudflarestorage.com',
       'OBS_R2_ACCESS_KEY_ID': 'test-access-SENTINEL', 'OBS_R2_SECRET_ACCESS_KEY': 'test-secret-SENTINEL',
       'OBS_R2_BUCKET': 'gwr-observation-test', 'GITHUB_REPOSITORY': 'synthetic/runtime',
       'GITHUB_RUN_ID': '100', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a'*40}
PRODUCER = Producer('synthetic/runtime', '100', 1, 'a'*40)
NAMES = ('synthetic/repo1', 'synthetic/repo2')


class Clock:
    def __init__(self, now=None):
        self.wall = now or B+timedelta(minutes=1)
    def now(self): return self.wall
    def monotonic(self): return self.wall.timestamp()
    def sleep(self, seconds): self.wall += timedelta(seconds=seconds)


class MemoryS3(ObservationS3):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.deleted = []

    def put_object(self, **kwargs):
        result = super().put_object(**kwargs)
        self.modified[kwargs['Key']] = self.clock.now()
        return {'ResponseMetadata': {'HTTPStatusCode': 200}, **result}

    def delete_object(self, Bucket, Key):
        self.send('delete', Key)
        self.deleted.append(Key)
        del self.objects[Key]
        return {'ResponseMetadata': {'HTTPStatusCode': 204}}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    def forbidden(*a, **kw): pytest.fail('REAL NETWORK/DB/SDK FORBIDDEN')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    import boto3
    monkeypatch.setattr(boto3, 'client', forbidden)


@pytest.fixture
def setup():
    clock = Clock()
    client = MemoryS3(clock)
    settings = runtime.ObservationTestSettings.from_environ(ENV)
    calls = []
    state = {'stars': 1000, 'forks': 100, 'failure': None, 'duration': 0}
    def respond(request):
        calls.append(request)
        if state['duration']:
            clock.sleep(state['duration'])
        if request.url.host == 'github.com':
            return httpx.Response(200, text=''.join(f'<h2><a href="/{name}">{name}</a></h2>' for name in NAMES))
        if state['failure'] and request.url.path.startswith('/repositories/'):
            return httpx.Response(state['failure'], headers={'x-ratelimit-remaining':'0'})
        tail = request.url.path.rsplit('/', 1)[1]
        identity = int(tail.removeprefix('repo'))
        return httpx.Response(200, headers={'x-ratelimit-remaining':'100'}, json={
            'id': identity, 'full_name': f'synthetic/repo{identity}', 'private': False,
            'html_url': f'https://github.com/synthetic/repo{identity}', 'created_at':'2020-01-01T00:00:00Z',
            'description':'AI agent developer tools', 'stargazers_count':state['stars'],
            'forks_count':state['forks'], 'topics':['ai-agent'], 'fork':False})
    http = httpx.Client(transport=httpx.MockTransport(respond))
    yield clock, client, settings, http, calls, state
    http.close()


def execute(setup, mode, **kw):
    clock, client, settings, http, calls, state = setup
    lines = []
    result = runtime.execute_test_observations(mode=mode, settings=settings, client=client, http=http,
        producer=PRODUCER, clock=clock, names=NAMES, emit=lines.append, **kw)
    return result, lines


def test_full_two_real_component_boundaries_reloaded_from_store(setup):
    (code, first), lines = execute(setup, 'full')
    assert code == 2  # Honest first-boundary missing previous, not full success.
    assert first.summary.current_confirmed == 2 and first.summary.previous_confirmed == 0
    clock, client, settings, http, calls, state = setup
    previous_bytes = dict(client.objects)
    clock.wall += timedelta(days=7)
    state.update(stars=1200, forks=150)
    (code, second), lines = execute(setup, 'full')
    assert code == 0
    assert second.namespace == 'fixture' and second.summary.star_comparisons_confirmed == 2
    a = second.for_repository(1)
    assert a.comparison.star_delta.value == 200 and a.comparison.star_growth.value == 20
    assert a.comparison.fork_delta.value == 50 and a.comparison.fork_growth.value == 50
    assert a.trending_rank.value == 1 and a.current.snapshot.provenance.sha256
    assert a.current.snapshot.provenance.object_key in client.objects
    assert a.current.snapshot.provenance.observed_at == clock.now()
    assert all(client.objects[key] == raw for key, raw in previous_bytes.items())
    assert any('Consumer source:' in line for line in lines)
    assert all('/search/' not in str(req.url) and '/graphql' not in str(req.url) for req in calls)


@pytest.mark.parametrize('mode', ['pool', 'boundary', 'verify', 'full'])
def test_modes_only_test_keys_and_no_production_changes(setup, mode):
    if mode == 'boundary': execute(setup, 'pool')
    client = setup[1]; protected = {k:v for k,v in client.objects.items()}
    (code, _), lines = execute(setup, mode)
    assert code in {0, 2}
    assert all(key.startswith(('test-observations/v1/', 'test-observation-pools/v1/')) for key in client.keys)
    assert all(client.objects[k] == v for k,v in protected.items())
    assert 'Production DB/Claim/Weekly Pipeline/OpenAI/Telegram: NOT ACCESSED' in lines


def test_verify_uses_no_github_or_factory(setup):
    execute(setup, 'full')
    setup[4].clear()
    (code, context), _ = execute(setup, 'verify')
    assert code == 2 and not setup[4] and context.summary.current_confirmed == 2


def test_boundary_without_pool_is_explicit_failure(setup):
    (code, _), lines = execute(setup, 'boundary')
    assert code == 1 and any('POOL_NOT_FOUND' in line for line in lines)
    assert not setup[4]


@pytest.mark.parametrize('seconds', [3601, 14400])
def test_late_modes_block_before_sdk_factory(seconds):
    clock = Clock(B+timedelta(seconds=seconds))
    with pytest.raises(runtime.TestRuntimeError, match='BOUNDARY_WINDOW_CLOSED'):
        runtime.run_manual(mode='full', environ=ENV, confirmed=True, clock=clock,
                           client_factory=lambda _: pytest.fail('SDK FACTORY FORBIDDEN'))


@pytest.mark.parametrize('seconds', [60, 600, 3600])
def test_permitted_actual_observation_window(setup, seconds):
    setup[0].wall = B+timedelta(seconds=seconds)
    (code, ctx), _ = execute(setup, 'full')
    assert ctx.summary.current_confirmed == 2
    assert ctx.for_repository(1).current.snapshot.provenance.observed_at == setup[0].now()


def test_request_crossing_window_is_not_adjusted(setup):
    execute(setup, 'pool')
    setup[0].wall = B+timedelta(minutes=59)
    setup[5]['duration'] = 120
    (code, _), lines = execute(setup, 'boundary')
    assert code == 2 and 'Boundary coverage: NONE' in lines
    assert any('20:' in line or '11:' in line for line in lines if 'Actual completion' in line)


def test_existing_pool_merge_deduplicates_and_preserves_first_seen(setup):
    execute(setup, 'pool')
    clock, client, settings, http, calls, state = setup
    first = clock.now()
    clock.wall += timedelta(days=1)
    execute(setup, 'pool')
    with R2ObservationStore(settings, client=runtime.TestBucketClient(client, settings), as_of=clock.now(),
            namespace='fixture', profile='report_read') as store:
        refs = store.list_recent_pools()
        latest = max(refs, key=lambda r:(r.last_modified,r.object_key))
        pool = store.get_pool(latest.boundary, latest.object_id).validation.pool
    assert len(pool.entries) == 2
    assert all(entry.record.first_seen_at == first for entry in pool.entries)
    assert all(not entry.record.meaningful_at and not entry.record.independent_signals for entry in pool.entries)


def test_expired_discovery_not_renewed_by_reobservation(setup):
    execute(setup, 'pool')
    setup[0].wall += timedelta(days=8)
    (code, _), lines = execute(setup, 'pool')
    assert code == 1 and any('NO_POOL_CANDIDATES' in line for line in lines)


def test_discovery_limit_and_trending_admission_only(setup):
    clock, client, settings, http, calls, state = setup
    discovery = runtime.PoolDiscovery(http, clock=clock)
    records, _ = discovery.records(())
    assert len(records) == 2 and discovery.trending_attempts == 1
    assert not any(row.independent_signals for row in records)
    with pytest.raises(runtime.TestRuntimeError, match='DISCOVERY_LIMIT'):
        runtime.parse_names(','.join(f'synthetic/repo{i}' for i in range(9)))


@pytest.mark.parametrize('status', [403, 404, 429])
def test_partial_metadata_failure_keeps_safe_diagnostics(setup, status):
    execute(setup, 'pool')
    setup[5]['failure'] = status
    (code, _), lines = execute(setup, 'boundary')
    assert code in {1,2}
    assert any('Observation collection: PARTIAL_SUCCESS' in line for line in lines)
    assert any('Boundary coverage: NONE' in line for line in lines)


@pytest.mark.parametrize('corrupt', ['json', 'checksum'])
def test_corrupt_stored_objects_unavailable_not_empty(setup, corrupt):
    execute(setup, 'full')
    setup[1].corrupt = corrupt
    (code, context), lines = execute(setup, 'verify')
    assert code == 1 and any(row.status == 'UNAVAILABLE' for row in context.coverage)
    assert context.summary.current_confirmed == 0


def test_incomplete_list_is_fail_closed(setup):
    setup[1].truncated = True
    (code, context), lines = execute(setup, 'verify')
    assert code == 1 and 'Diagnostic: INCOMPLETE_LIST: 2' in lines


def test_permission_failure_not_fresh_empty_pool(setup, monkeypatch):
    def denied(**kw):
        setup[1].meta.events.fire()
        raise FakeClientError('AccessDenied',403)
    monkeypatch.setattr(setup[1], 'list_objects_v2', denied)
    (code, _), lines = execute(setup, 'pool')
    assert code == 1 and not setup[4]
    assert any('AUTHENTICATION_OR_PERMISSION' in line for line in lines)


@pytest.mark.parametrize('fault,expected', [('commit-timeout',0), ('drop',1)])
def test_pool_timeout_same_key_reconciliation(setup, fault, expected):
    setup[1].put_mode = fault
    (code, _), lines = execute(setup, 'pool')
    assert code == expected
    assert len({key for key in setup[1].keys if key.endswith('.json')}) == 1


def test_cleanup_only_this_runs_owned_keys(setup):
    execute(setup, 'full')
    old = dict(setup[1].objects)
    setup[0].wall += timedelta(seconds=1)
    (code, ctx), lines = execute(setup, 'full', cleanup=True)
    assert len(setup[1].deleted) == 2
    assert not set(setup[1].deleted) & set(old)
    assert setup[1].objects == old


def test_cleanup_failure_is_non_success(setup, monkeypatch):
    def denied(**kw): raise FakeClientError('AccessDenied',403)
    monkeypatch.setattr(setup[1], 'delete_object', denied)
    (code, _), lines = execute(setup, 'pool', cleanup=True)
    assert code == 2 and any('CLEANUP_WARNING' in line for line in lines)


def test_lost_ack_cleanup_reports_skipped_uncertain_object(setup):
    setup[1].put_mode = 'commit-timeout'
    (code, _), lines = execute(setup, 'pool', cleanup=True)
    assert code == 2 and any('CLEANUP_WARNING_UNCONFIRMED_WRITE' in line for line in lines)
    assert not setup[1].deleted


def test_scope_blocks_prod_prefix_and_cross_bucket(setup):
    client = runtime.TestBucketClient(setup[1], setup[2])
    for key in ('production/github_weekly_report.db', 'backups/a.db', 'observations/v1/production/a',
                'test-observations/v1/../../production/x'):
        with pytest.raises(R2ObservationError, match='TEST_NAMESPACE_REQUIRED'):
            client.get_object(Bucket=ENV['OBS_R2_BUCKET'], Key=key)
    with pytest.raises(R2ObservationError):
        client.list_objects_v2(Bucket='production', Prefix='test-observations/v1/2026-11-01/')


def test_same_time_different_values_surface_consumer_conflict(setup):
    execute(setup, 'full')
    old_objects = dict(setup[1].objects)
    setup[5]['stars'] = 1200
    (code, context), lines = execute(setup, 'full')
    assert code == 1 and context.summary.conflicted_repositories == 2
    assert all(setup[1].objects[key] == raw for key,raw in old_objects.items())


def test_same_key_idempotency_and_immutable_conflict(setup):
    from dataclasses import replace
    from services.observation_validation import seal_observation
    execute(setup, 'full')
    clock, client, settings, *_ = setup
    key, raw = next((key,raw) for key,raw in client.objects.items() if key.startswith('test-observations/'))
    envelope = validate_observation(raw, now=clock.now(), namespace='fixture').envelope
    scoped = runtime.TestBucketClient(client, settings)
    with R2ObservationStore(settings, client=scoped, as_of=clock.now(), namespace='fixture') as store:
        assert store.publish_observation(envelope).publication_status == 'VERIFIED'
    assert not scoped.owned  # An existing identical key does not confer cleanup ownership.
    row = replace(envelope.payload.repositories[0], stars=1200)
    changed = seal_observation(replace(envelope.payload, repositories=(row, *envelope.payload.repositories[1:])))
    with R2ObservationStore(settings, client=scoped, as_of=clock.now(), namespace='fixture') as store:
        with pytest.raises(R2ObservationError, match='IMMUTABLE_OBJECT_CONFLICT'):
            store.publish_observation(changed)
    assert client.objects[key] == raw


def test_cutoff_later_objects_are_excluded(setup):
    execute(setup, 'full')
    for key in setup[1].modified:
        setup[1].modified[key] += timedelta(seconds=1)
    (code, context), _ = execute(setup, 'verify')
    assert code == 2 and context.summary.current_confirmed == 0
    assert all(row.status == 'EMPTY' for row in context.coverage)


def test_private_metadata_cannot_enter_pool_or_boundary(setup):
    private = {'id':1,'private':True}
    http = httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=private)))
    try:
        discovery = runtime.PoolDiscovery(http, clock=setup[0])
        records, failures = discovery.records(NAMES)
        assert not records and failures
        adapter = runtime.PublicObservationGitHub(http, clock=setup[0])
        from collectors.github_client import GitHubError
        with pytest.raises(GitHubError, match='Public repository'):
            adapter.repository(1)
    finally:
        http.close()


def test_invalid_producer_before_sdk_creation():
    with pytest.raises(runtime.TestRuntimeError, match='INVALID_PRODUCER'):
        runtime.run_manual(mode='verify', environ={**ENV,'GITHUB_SHA':'invalid'}, confirmed=True,
                           client_factory=lambda _:pytest.fail('SDK FACTORY FORBIDDEN'))


def test_discovery_endpoint_budget_and_reserve(setup):
    from collectors.github_client import ApiBudgetExceeded
    discovery = runtime.PoolDiscovery(setup[3], clock=setup[0])
    discovery.remaining = 5
    with pytest.raises(ApiBudgetExceeded):
        discovery.request('GET', 'https://api.github.com/repos/synthetic/repo1')
    discovery.remaining = None
    discovery.attempts = 12
    with pytest.raises(ApiBudgetExceeded):
        discovery.request('GET', 'https://api.github.com/repos/synthetic/repo1')
    with pytest.raises(ApiBudgetExceeded):
        discovery.request('POST', 'https://api.github.com/graphql')


def test_namespace_not_accepted_by_production_validator(setup):
    execute(setup, 'full')
    raw = next(raw for key,raw in setup[1].objects.items() if key.startswith('test-observations/'))
    with pytest.raises(ObservationValidationError, match='FIXTURE'):
        validate_observation(raw, now=setup[0].now(), namespace='production')


@pytest.mark.parametrize('name', runtime.ENV_NAMES)
def test_no_settings_fallback(name):
    env = {**ENV, **{k.removeprefix('OBS_'):v for k,v in ENV.items() if k.startswith('OBS_')}}
    del env[name]
    with pytest.raises(runtime.TestRuntimeError, match='MISSING_ENV: '+name):
        runtime.ObservationTestSettings.from_environ(env)


@pytest.mark.parametrize('bucket', ['gwr-production', 'production', 'gwr-observation-test-../production'])
def test_reject_non_test_bucket(bucket):
    with pytest.raises(runtime.TestRuntimeError, match='ISOLATED_BUCKET'):
        runtime.ObservationTestSettings.from_environ({**ENV,'OBS_R2_BUCKET':bucket})


def test_validate_does_not_read_settings_or_invoke_client():
    class Forbidden(dict):
        def get(self,*a): pytest.fail('ENV READ FORBIDDEN')
    assert main([], environ=Forbidden(), client_factory=lambda _: pytest.fail('CLIENT')) == 0


def test_confirmation_required_before_settings():
    class Forbidden(dict):
        def get(self,*a): pytest.fail('ENV READ FORBIDDEN')
    assert main(['--mode','pool'], environ=Forbidden()) == 1


def test_script_live_with_only_fake_factories_and_no_secret_output(setup):
    lines = []
    code = main(['--mode','full','--confirm-isolated-bucket','--repositories',','.join(NAMES)],
        environ=ENV, client_factory=lambda _: setup[1], http_factory=lambda:setup[3],
        clock=setup[0], emit=lines.append)
    assert code == 2
    assert all(value not in '\n'.join(lines) for key,value in ENV.items() if key.startswith('OBS_'))


def test_workflow_manual_isolated_modes_and_protected_workflows():
    data = yaml.safe_load((ROOT/'.github/workflows/observation-test-runtime.yml').read_text())
    assert set(data['on']) == {'workflow_dispatch'}
    assert data['on']['workflow_dispatch']['inputs']['mode']['options'] == ['validate','pool','boundary','verify','full']
    assert data['permissions'] == {'contents':'read'}
    assert data['concurrency']['cancel-in-progress'] is False
    assert data['concurrency']['queue'] == 'max'
    validate, live = data['jobs']['validate'], data['jobs']['isolated-live']
    assert 'secrets.' not in str(validate) and 'environment' not in validate
    assert live['environment'] == 'observation-r2-test'
    assert 'exit 1' in live['steps'][0]['run']
    env = live['steps'][-1]['env']
    assert {k for k,v in env.items() if 'secrets.' in v} == set(runtime.ENV_NAMES)
    assert env['GITHUB_TOKEN'] == '${{ github.token }}'
    assert all('OPENAI' not in k and 'TELEGRAM' not in k for k in env)
    for name in ('weekly-report.yml','observation.yml','observation-r2-check.yml'):
        old = yaml.safe_load((ROOT/'.github/workflows'/name).read_text(encoding='utf-8'))
        assert old['concurrency']['group'] != data['concurrency']['group']
    assert 'upload-artifact' not in str(data)
    assert 'run_weekly' not in str(data)
