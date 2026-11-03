"""Manual real-data observation runtime, isolated from weekly production.

The schema's existing fixture namespace denotes test routing here, not synthetic
GitHub measurements. No SQLite, report, claim, or production settings are loaded.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import timezone
import re
import time

import httpx

from collectors.github_client import GitHubClient, GitHubError, ApiBudgetExceeded
from collectors.github_repository import repository_model
from collectors.github_trending import collect_trending
from config import observation_rules as rules
from models.observation import Producer
from services.observation_consumer import consume_observations
from services.observation_job import Clock, ObservationGitHub, run_boundary_observation, _metadata_row
from services.observation_pool import PoolRecord, PoolValidationError, build_pool
from services.observation_pool_validation import seal_pool
from services.observation_r2_probe import ENDPOINT, ENV_NAMES, create_probe_client, validate_sdk_contract, safe_error
from services.observation_timing import observation_allowed
from services.observation_validation import _name, _time, ObservationValidationError
from services.r2_observation_store import R2ObservationStore, R2ObservationError
from services.scheduler import report_period


class TestRuntimeError(ValueError):
    __test__ = False

    def __init__(self, category):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class ObservationTestSettings:
    endpoint: str = field(repr=False)
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    bucket: str = field(repr=False)

    @classmethod
    def from_environ(cls, environ):
        values = []
        for name in ENV_NAMES:
            value = environ.get(name, '')
            if not isinstance(value, str) or not value.strip():
                raise TestRuntimeError('MISSING_ENV: '+name)
            values.append(value)
        settings = cls(*values)
        settings.validate_r2()
        return settings

    @property
    def r2_bucket(self):
        return self.bucket

    def validate_r2(self):
        if not ENDPOINT.fullmatch(self.endpoint):
            raise TestRuntimeError('INVALID_OBS_ENDPOINT')
        if (not re.fullmatch(r'gwr-observation-test(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?', self.bucket)
                or len(self.bucket) > 63):
            raise TestRuntimeError('ISOLATED_BUCKET_REQUIRED')
        if not self.access_key.strip() or not self.secret_key.strip():
            raise TestRuntimeError('MISSING_OBS_CREDENTIALS')


class TestBucketClient:
    """Defense in depth for injected SDK client; only two test JSON namespaces.

    No raw key is accepted from CLI. Cleanup is limited to acknowledged PUTs by
    this invocation and exact read-back bytes; 412/uncertain writes are not owned.
    """
    __test__ = False

    def __init__(self, client, settings):
        self.client, self.settings, self.meta = client, settings, client.meta
        self.owned = {}
        self.uncertain_writes = set()
        self.counts = Counter()
        self.http_statuses = Counter()

    def _sdk(self, method, kwargs):
        try:
            response = getattr(self.client, method)(**kwargs)
        except Exception as error:
            self.http_statuses[(method, safe_error(error).status)] += 1
            raise
        status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
        self.http_statuses[(method, status if type(status) is int else None)] += 1
        return response

    def _scope(self, kwargs, *, listing=False):
        target = kwargs.get('Prefix' if listing else 'Key', '')
        ending = r'\d{4}-\d{2}-\d{2}/' + ('' if listing else r'[0-9a-f-]{36}\.json')
        if kwargs.get('Bucket') != self.settings.bucket or not re.fullmatch(
                r'test-(?:observations|observation-pools)/v1/'+ending, target):
            raise R2ObservationError('scope', 'TEST_NAMESPACE_REQUIRED')

    def list_objects_v2(self, **kwargs):
        self._scope(kwargs, listing=True)
        self.counts['list'] += 1
        return self._sdk('list_objects_v2', kwargs)

    def get_object(self, **kwargs):
        self._scope(kwargs)
        self.counts['get'] += 1
        return self._sdk('get_object', kwargs)

    def put_object(self, **kwargs):
        self._scope(kwargs)
        if kwargs.get('IfNoneMatch') != '*':
            raise R2ObservationError('scope', 'CONDITIONAL_PUT_REQUIRED')
        self.counts['put'] += 1
        try:
            response = self._sdk('put_object', kwargs)
        except Exception as error:
            safe = safe_error(error)
            if safe.category in {'NETWORK_ERROR', 'TIMEOUT'} or (safe.status is not None and safe.status >= 500):
                self.uncertain_writes.add(kwargs['Key'])
            raise
        if response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 200:
            self.owned[kwargs['Key']] = kwargs['Body']
        return response

    def cleanup(self):
        warnings = ['CLEANUP_WARNING_UNCONFIRMED_WRITE' for _ in self.uncertain_writes]
        for key, expected in self.owned.items():
            try:
                # At most two objects (one Pool and one Observation) per run.
                self.counts['cleanup_get'] += 1
                response = self._sdk('get_object', {'Bucket':self.settings.bucket, 'Key':key})
                try:
                    raw = response['Body'].read(rules.MAX_JSON_BYTES+1)
                finally:
                    response['Body'].close()
                if raw != expected:
                    raise TestRuntimeError('CLEANUP_OWNERSHIP_MISMATCH')
                self.counts['cleanup_delete'] += 1
                result = self._sdk('delete_object', {'Bucket':self.settings.bucket, 'Key':key})
                if result.get('ResponseMetadata', {}).get('HTTPStatusCode') != 204:
                    raise TestRuntimeError('CLEANUP_NOT_CONFIRMED')
            except Exception:
                warnings.append('CLEANUP_WARNING')
        return tuple(warnings)


class PoolDiscovery:
    """Public GET metadata <=8 logical/12 attempts; no Search/GraphQL."""
    def __init__(self, http, *, token='', clock=None):
        self.http, self.clock = http, clock or Clock()
        self.attempts = self.lookups = self.trending_attempts = 0
        self.remaining = None
        self.github = GitHubClient(token, client=self, sleep=getattr(self.clock, 'sleep', time.sleep))

    def request(self, method, url, **kwargs):
        path = url.removeprefix('https://api.github.com/repos/')
        if method != 'GET' or url == path:
            raise ApiBudgetExceeded('Public discovery endpoint required')
        _name(path)
        if self.attempts >= 12 or (self.remaining is not None and self.remaining <= rules.CORE_RESERVE):
            raise ApiBudgetExceeded('Discovery budget/reserve')
        self.attempts += 1
        response = self.http.request(method, url, **kwargs, timeout=rules.HTTP_TIMEOUT_SECONDS,
                                     follow_redirects=False)
        value = response.headers.get('x-ratelimit-remaining', '')
        if value.isdigit():
            self.remaining = int(value)
        return response

    def get(self, url):
        if url != 'https://github.com/trending?since=weekly' or self.trending_attempts >= 1:
            raise ApiBudgetExceeded('Discovery Trending budget')
        self.trending_attempts += 1
        return self.http.get(url, timeout=rules.HTTP_TIMEOUT_SECONDS, follow_redirects=False)

    def records(self, names):
        # A discovery-page observation is admission only, never boundary rank.
        if not names:
            names = tuple(name for name, _ in collect_trending(self))[:rules.EXPLORATION_LIMIT]
        rows, failures = [], []
        for name in dict.fromkeys(names):
            if self.lookups >= rules.EXPLORATION_LIMIT:
                break
            self.lookups += 1
            try:
                data = self.github.repository(name)
                observed = _time(self.clock.now())
                if data.get('private') is not False:
                    raise TestRuntimeError('PUBLIC_REPOSITORY_REQUIRED')
                metadata, _, _ = _metadata_row(data['id'], data, observed)
                model = repository_model(data)
                rows.append(PoolRecord(metadata.github_id, metadata.full_name, observed, observed,
                    report_period(observed).end.astimezone(timezone.utc), verified=True, fixture=True,
                    relevant=model.relevant, excluded=bool(model.excluded_reason)))
            except (GitHubError, httpx.HTTPError, ValueError, KeyError, TypeError):
                failures.append('DISCOVERY_SOURCE_UNAVAILABLE')
        return rows, failures


def parse_names(value):
    names = tuple(dict.fromkeys(part.strip() for part in value.split(',') if part.strip()))
    if len(names) > rules.EXPLORATION_LIMIT:
        raise TestRuntimeError('DISCOVERY_LIMIT_EXCEEDED')
    for name in names:
        _name(name)
    return names


class PublicObservationGitHub(ObservationGitHub):
    """The manual test credential must never turn private metadata into evidence."""
    def repository(self, identity):
        data = super().repository(identity)
        if data.get('private') is not False:
            raise GitHubError('Public repository metadata required')
        return data


def _validate_producer(producer):
    _name(producer.repository)
    if (not re.fullmatch(r'[0-9]{1,24}', producer.run_id) or type(producer.attempt) is not int
            or producer.attempt < 1 or not re.fullmatch(r'[0-9a-f]{7,64}', producer.code_version)):
        raise TestRuntimeError('INVALID_PRODUCER')


def publish_test_pool(*, settings, client, discovery, producer, clock, names=()):
    cutoff = _time(clock.now())
    records = []
    # Exactly the newest manifest per completed week, max four GETs.
    with R2ObservationStore(settings, client=client, as_of=cutoff, namespace='fixture', profile='report_read') as store:
        refs = store.list_recent_pools()
        weeks = {}
        for ref in sorted(refs, key=lambda ref: (ref.last_modified, ref.object_key), reverse=True):
            weeks.setdefault(ref.boundary, ref)
        for ref in weeks.values():
            stored = store.get_pool(ref.boundary, ref.object_id)
            if stored is None:
                raise TestRuntimeError('POOL_DISAPPEARED')
            records.extend(stored.validation.pool.entries)
    new, failures = discovery.records(names)
    records.extend(new)
    now = _time(clock.now())
    pool = build_pool(records, as_of=now, fixture=True)
    if pool.status != 'READY' or not pool.entries:
        raise TestRuntimeError(pool.status if pool.status != 'READY' else 'NO_POOL_CANDIDATES')
    from uuid import uuid4
    envelope = seal_pool(pool, pool_id=str(uuid4()), created_at=now, producer=producer)
    with R2ObservationStore(settings, client=client, as_of=now, namespace='fixture') as store:
        receipt = store.publish_pool(envelope)
    return pool, receipt, tuple(failures)


def execute_test_observations(*, mode, settings, producer, client, http=None, token='',
                              clock=None, names=(), cleanup=False, emit=print):
    """Real components; injected SDK/HTTP/clock for entirely offline E2E tests."""
    if mode not in {'pool', 'boundary', 'verify', 'full'}:
        raise TestRuntimeError('INVALID_MODE')
    settings.validate_r2()
    clock = clock or Clock()
    scoped = TestBucketClient(client, settings)
    # Validate producer before any I/O (same existing typed contract).
    _validate_producer(producer)
    started = _time(clock.now())
    period = report_period(started)
    if mode in {'boundary', 'full'} and not observation_allowed(started, period.end):
        raise TestRuntimeError('BOUNDARY_WINDOW_CLOSED')
    emit('Mode: '+mode+'; data_origin=live-github; namespace=fixture (test routing)')
    emit(f'Producer: {producer.run_id}/{producer.attempt}')
    emit('Actual start: '+started.isoformat())
    emit('Boundary: '+period.end.isoformat())
    code = 0
    context = None
    diagnostics = []
    remaining = None
    try:
        if mode in {'pool', 'full'}:
            discovery = PoolDiscovery(http, token=token, clock=clock)
            pool, receipt, failures = publish_test_pool(settings=settings, client=scoped,
                discovery=discovery, producer=producer, clock=clock, names=names)
            diagnostics.extend(failures)
            remaining = discovery.remaining
            emit(f'Pool repositories: {len(pool.entries)}')
            emit(f'Discovery REST attempts: {discovery.attempts}; HTML: {discovery.trending_attempts}; Search/GraphQL: 0')
            _emit_receipt(receipt, emit)
            if receipt.publication_status != 'VERIFIED':
                raise TestRuntimeError('POOL_PUBLICATION_UNCONFIRMED')
        if mode in {'boundary', 'full'}:
            now = _time(clock.now())
            if not observation_allowed(now, period.end):
                raise TestRuntimeError('BOUNDARY_WINDOW_CLOSED')
            with R2ObservationStore(settings, client=scoped, as_of=now,
                    namespace='fixture', profile='observation_job') as store:
                github = PublicObservationGitHub(http, clock=clock, token=token)
                github.remaining = remaining  # Preserve the same-run core reserve across stages.
                result = run_boundary_observation(store=store, github=github, producer=producer, clock=clock)
            emit('Observation collection: '+result.status)
            diagnostics.extend(result.diagnostics)
            emit(f'Boundary REST attempts: {github.rest_attempts}; HTML: {github.trending_attempts}; Search/GraphQL: 0')
            if result.envelope:
                payload = result.envelope.payload
                emit('Actual completion: '+payload.completed_at.isoformat())
                emit('Boundary coverage: '+payload.boundary_coverage)
                emit(f'Pool source: {payload.pool_ref.key}; SHA-256={payload.pool_ref.sha256}')
                for row in payload.repositories:
                    emit(f'Repository {row.github_id}: {row.status}; HTTP={row.diagnostics.http_status}; request={row.request_started_at}; observed={row.observed_at}')
                if payload.boundary_coverage == 'NONE':
                    code = 2
            if result.receipt:
                _emit_receipt(result.receipt, emit)
            if result.status == 'FAILED':
                raise TestRuntimeError('BOUNDARY_JOB_FAILED')
            if result.status == 'PARTIAL_SUCCESS':
                code = 2
        if mode in {'verify', 'full'}:
            cutoff = _time(clock.now())
            with R2ObservationStore(settings, client=scoped, as_of=cutoff,
                    namespace='fixture', profile='report_read') as store:
                context = consume_observations(store=store, period=report_period(cutoff), cutoff=cutoff)
            emit('Consumer cutoff: '+cutoff.isoformat())
            emit('Coverage: '+str(context.summary))
            for coverage in context.coverage:
                emit(f'Boundary read: {coverage.boundary.isoformat()}; {coverage.status}; loaded={coverage.loaded_objects}')
            for repository in context.repositories:
                for selection in (repository.current, repository.previous):
                    if selection.snapshot:
                        source = selection.snapshot.provenance
                        emit(f'Consumer source: {source.object_key}; SHA-256={source.sha256}; observed={source.observed_at.isoformat()}')
            diagnostics.extend(d.category for d in context.diagnostics)
            if context.summary.unavailable_repositories or context.summary.conflicted_repositories or any(
                    row.status == 'UNAVAILABLE' for row in context.coverage) or context.trending.status == 'CONFLICT':
                raise TestRuntimeError('CONSUMER_UNAVAILABLE_OR_CONFLICT')
            if not context.summary.current_confirmed:
                code = 2
        if diagnostics:
            code = code or 2
    except Exception as error:
        category = error.category if isinstance(error, (TestRuntimeError, PoolValidationError, R2ObservationError, ObservationValidationError)) else safe_error(error).category
        diagnostics.append(category)
        code = 1
    finally:
        if cleanup:
            warnings = scoped.cleanup()
            diagnostics.extend(warnings)
            emit('Cleanup: '+('CLEANUP_WARNING' if warnings else 'SUCCESS (acknowledged objects only)'))
            if warnings:
                code = code or 2
        else:
            emit('Cleanup: RETAINED (test observations needed for later comparison)')
        emit('R2 logical operations: '+str(dict(scoped.counts)))
        for (operation, status), count in sorted(scoped.http_statuses.items(), key=lambda item:str(item[0])):
            emit(f'R2 HTTP: {operation}; {status if status is not None else "NOT AVAILABLE"}; attempts={count}')
        for category, count in sorted(Counter(diagnostics).items()):
            emit(f'Diagnostic: {category}: {count}')
        emit('Production DB/Claim/Weekly Pipeline/OpenAI/Telegram: NOT ACCESSED')
        emit('Final Status: '+('SUCCESS' if code == 0 else 'PARTIAL_OR_MISSING' if code == 2 else 'FAILED'))
    return code, context


def _emit_receipt(receipt, emit):
    emit(f'Publication: {receipt.publication_status}; key={receipt.object_key}; SHA-256={receipt.sha256}')
    if receipt.error_category:
        emit(f'Publication diagnostic: {receipt.error_category}; HTTP={receipt.http_status}')


def run_manual(*, mode, environ, confirmed=False, names=(), cleanup=False,
               client_factory=create_probe_client, http_factory=None, clock=None, emit=print):
    """Only explicit live CLI invokes client factories; no settings fallback."""
    if confirmed is not True:
        raise TestRuntimeError('EXPLICIT_ISOLATED_BUCKET_CONFIRMATION_REQUIRED')
    settings = ObservationTestSettings.from_environ(environ)
    producer = Producer(environ.get('GITHUB_REPOSITORY', ''), environ.get('GITHUB_RUN_ID', ''),
        int(environ.get('GITHUB_RUN_ATTEMPT', '0')), environ.get('GITHUB_SHA', ''))
    _validate_producer(producer)
    clock = clock or Clock()
    # Gate late execution before even creating a client, not just before GET.
    if mode in {'boundary', 'full'} and not observation_allowed(clock.now(), report_period(clock.now()).end):
        raise TestRuntimeError('BOUNDARY_WINDOW_CLOSED')
    validate_sdk_contract()
    client = client_factory(settings)
    try:
        if mode == 'verify':
            return execute_test_observations(mode=mode, settings=settings, producer=producer,
                client=client, clock=clock, cleanup=cleanup, emit=emit)[0]
        factory = http_factory or (lambda: httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False,
                                                       follow_redirects=False, timeout=rules.HTTP_TIMEOUT_SECONDS))
        with factory() as http:
            return execute_test_observations(mode=mode, settings=settings, producer=producer,
                client=client, http=http, token=environ.get('GITHUB_TOKEN', ''), clock=clock,
                names=names, cleanup=cleanup, emit=emit)[0]
    finally:
        client.close()
