"""Dependency-injected boundary job. No clients, settings or network on import.

The CLI deliberately has no production activation path in Phase 4.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import time
from uuid import uuid4

import httpx

from config import observation_rules as rules
from collectors.github_client import GitHubClient, GitHubError, RateLimitError, ApiBudgetExceeded
from collectors.github_trending import collect_trending
from models.observation import (
    ObservationPayload, RepositoryMetadata, RepositoryObservation, TrendingObservation,
    TrendingEntry, SafeDiagnostics, PoolReference, ObservationEnvelope, Producer,
)
from models.observation_storage import PublicationReceipt
from services.observation_validation import (
    _time, _metadata, _decode, _integer, _name, _digest, ObservationValidationError,
    seal_observation, validate_observation,
)
from services.observation_pool_validation import validate_pool
from services.observation_timing import observation_allowed
from services.r2_observation_store import R2ObservationError, R2ObservationStore
from services.scheduler import report_period


class ObservationJobError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__('Observation job: '+category)


class Clock:
    def now(self): return datetime.now(timezone.utc)
    def monotonic(self): return time.monotonic()


class ObservationGitHub:
    """Separate bounded adapter; existing production GitHubClient is unchanged.

    Caller supplies a fresh HTTP client with transport retries/redirects disabled.
    Every GitHubClient retry traverses this adapter's physical-attempt gate.
    """
    def __init__(self, http, *, clock, token='', can_collect=lambda: True):
        # HTTPTransport retries happen below Client.request and cannot be counted
        # here. Reject opaque/retrying transports rather than claiming a bound.
        if not isinstance(http,httpx.Client):
            raise ObservationJobError('UNSUPPORTED_HTTP_RETRY_POLICY')
        transports = [getattr(http,'_transport',None),*getattr(http,'_mounts',{}).values()]
        for transport in transports:
            if transport is None or isinstance(transport,httpx.MockTransport): continue
            if not isinstance(transport,httpx.HTTPTransport) or getattr(transport._pool,'_retries',None) != 0:
                raise ObservationJobError('UNSUPPORTED_HTTP_RETRY_POLICY')
        self.http, self.clock, self.can_collect = http, clock, can_collect
        self.rest_attempts = self.metadata_requests = self.trending_attempts = 0
        self.remaining = None
        self.last_response = None
        self.transport_failed = False
        self.github = GitHubClient(token, client=self, sleep=self._sleep)

    def _sleep(self, seconds):
        # No secondary-limit retry. Only bounded transport/server retries use this.
        if not self.can_collect(): raise ApiBudgetExceeded('Observation deadline')
        # Clock injection makes backoff deterministic without wall-clock sleeping.
        if hasattr(self.clock, 'sleep'): self.clock.sleep(seconds)
        else: time.sleep(seconds)

    def request(self, method, url, **kwargs):
        if (method != 'GET' or not url.startswith('https://api.github.com/repositories/')
                or not url.removeprefix('https://api.github.com/repositories/').isdigit()):
            raise ApiBudgetExceeded('Observation endpoint forbidden')
        if (not self.can_collect() or self.rest_attempts >= rules.REST_ATTEMPT_LIMIT
                or (self.remaining is not None and self.remaining <= rules.CORE_RESERVE)):
            raise ApiBudgetExceeded('Observation request budget/reserve')
        self.rest_attempts += 1
        self.last_response = None
        self.transport_failed = False
        try:
            response = self.http.request(method,url,**kwargs,timeout=rules.HTTP_TIMEOUT_SECONDS,
                                         follow_redirects=False)
        except httpx.TransportError:
            self.transport_failed = True
            raise
        self.last_response = response
        value = response.headers.get('x-ratelimit-remaining')
        if value is not None and value.isdigit(): self.remaining = int(value)
        return response

    def repository(self, identity):
        _integer(identity,1)
        if self.metadata_requests >= rules.METADATA_REQUEST_LIMIT:
            raise ApiBudgetExceeded('Observation metadata budget')
        self.metadata_requests += 1
        self.last_response = None
        return self.github.request(f'repositories/{identity}')

    def get(self, url):
        # collect_trending is reused with a one-logical-fetch transport wrapper.
        if url != 'https://github.com/trending?since=weekly':
            raise ApiBudgetExceeded('Observation endpoint forbidden')
        for attempt in range(rules.TRENDING_ATTEMPT_LIMIT):
            if not self.can_collect() or self.trending_attempts >= rules.TRENDING_ATTEMPT_LIMIT:
                raise ApiBudgetExceeded('Observation Trending budget')
            self.trending_attempts += 1
            self.last_response = None
            self.transport_failed = False
            try:
                response = self.http.get(url,timeout=rules.HTTP_TIMEOUT_SECONDS,follow_redirects=False)
            except httpx.TransportError:
                self.transport_failed = True
                if attempt+1 == rules.TRENDING_ATTEMPT_LIMIT: raise
                self._sleep(1)
                continue
            self.last_response = response
            # Do not retry 403/429 or other HTTP errors immediately.
            return response

    def trending(self):
        self.last_response = None
        rows = collect_trending(self)
        return rows, hashlib.sha256(self.last_response.content).hexdigest()


@dataclass(frozen=True)
class ObservationJobResult:
    status: str
    boundary: datetime
    envelope: ObservationEnvelope | None = None
    receipt: PublicationReceipt | None = None
    diagnostics: tuple[str, ...] = ()
    published_at: datetime | None = None


def _failure(error, response, transport_failed=False):
    status = response.status_code if response is not None else None
    if isinstance(error, ApiBudgetExceeded): return 'BUDGET_EXHAUSTED'
    if isinstance(error, RateLimitError) or status == 429: return 'RATE_LIMIT'
    if status in {401,403}: return 'AUTHENTICATION_OR_PERMISSION'
    if status == 404: return 'NOT_FOUND'
    if isinstance(error, httpx.TransportError) or transport_failed: return 'NETWORK_ERROR'
    if status is not None and status >= 400: return 'HTTP_ERROR'
    return 'INVALID_RESPONSE'


def _diagnostic(category, response, duration_ms):
    headers = response.headers if response is not None else {}
    def number(name):
        value = headers.get(name)
        return int(value) if value is not None and value.isdigit() else None
    resource = headers.get('x-ratelimit-resource')
    return SafeDiagnostics(category=category,
        http_status=response.status_code if response is not None else None,
        rate_limit_resource=resource if resource in {'core','search','graphql'} else None,
        rate_limit_limit=number('x-ratelimit-limit'),rate_limit_remaining=number('x-ratelimit-remaining'),
        rate_limit_reset=number('x-ratelimit-reset'),retry_after=number('retry-after'),
        duration_ms=duration_ms)


def _metadata_row(identity, data, observed):
    meta = RepositoryMetadata(data['id'],data['full_name'],data['html_url'],_time(data['created_at']),
        description=(data.get('description') or '')[:rules.MAX_TEXT_LENGTH],
        topics=tuple(data.get('topics',())),language=data.get('language'),
        pushed_at=_time(data['pushed_at']) if data.get('pushed_at') else None,
        archived=data.get('archived',False),disabled=data.get('disabled',False))
    _metadata(_decode(meta),identity,observed)
    return meta,_integer(data['stargazers_count']),_integer(data['forks_count'])


def run_boundary_observation(*, store: R2ObservationStore, github: ObservationGitHub,
                             producer: Producer, clock=None, observation_id=None) -> ObservationJobResult:
    """One job, one store budget, no SQLite/Claim/report side effects.

    Return safe failure on absent/corrupt pool; do not substitute discoveries.
    Pool selection cutoff stays at job start; publishing advances the same store.
    """
    clock = clock or Clock()
    started = _time(clock.now())
    boundary = report_period(started).end.astimezone(timezone.utc)
    deadline = clock.monotonic()+rules.JOB_DEADLINE_SECONDS
    def can_collect():
        return clock.monotonic() < deadline-rules.PUBLICATION_RESERVE_SECONDS
    def r2_guard():
        # Existing SDK connect10/read60: don't start another physical attempt
        # without that headroom. This is cooperative, not process preemption.
        if clock.monotonic()+70 >= deadline:
            raise R2ObservationError('request','REQUEST_BUDGET_EXHAUSTED')
    if (store.profile != 'observation_job' or store.boundary != boundary or store.as_of != started
            or store.logical_requests or store.physical_attempts
            or github.rest_attempts or github.metadata_requests or github.trending_attempts):
        raise ObservationJobError('INVALID_STORE_CONTEXT')
    github.can_collect = can_collect
    # Guard is installed once for Pool List/GET and publication alike.
    store.set_request_guard(r2_guard)
    try:
        refs = store.list_recent_pools()
        refs = sorted(refs,key=lambda r:(r.boundary,r.last_modified,r.object_key),reverse=True)
        if not refs: return ObservationJobResult('FAILED',boundary,diagnostics=('POOL_NOT_FOUND',))
        selected = None
        # The latest manifest is authoritative. Never resurrect repositories
        # removed by a newer empty/expired/overflow Pool using an older object.
        ref = refs[0]
        stored = store.get_pool(ref.boundary,ref.object_id)
        if stored is None: raise ObservationJobError('POOL_DISAPPEARED')
        valid = validate_pool(stored.validation.envelope,now=started,namespace=store.namespace)
        if valid.pool.status != 'READY': raise ObservationJobError('POOL_NOT_READY')
        # Revalidate TTL at actual job time without renewing any timestamp.
        entries = tuple(e for e in valid.pool.entries if e.expires_at >= started)
        selected = stored,entries
        if selected is None or not selected[1]:
            return ObservationJobResult('FAILED',boundary,diagnostics=('POOL_EMPTY',))
    except (R2ObservationError,ObservationValidationError,ObservationJobError) as error:
        return ObservationJobResult('FAILED',boundary,diagnostics=(error.category,))
    stored,entries = selected
    rows = []
    stop_category = None
    for entry in entries:
        identity = entry.record.github_id
        url = f'https://api.github.com/repositories/{identity}'
        if not can_collect() or stop_category:
            rows.append(RepositoryObservation(identity,url,'unavailable',None,None,None,
                diagnostics=SafeDiagnostics(category=stop_category or 'BUDGET_EXHAUSTED')))
            continue
        request = _time(clock.now())
        request_tick = clock.monotonic()
        github.last_response = None
        try:
            data = github.repository(identity)
            response_at = _time(clock.now())
            duration_ms = round((clock.monotonic()-request_tick)*1000)
            meta,stars,forks = _metadata_row(identity,data,response_at)
            diag = _diagnostic(None,github.last_response,duration_ms)
            rows.append(RepositoryObservation(identity,url,'confirmed',request,response_at,response_at,
                                               meta,stars,forks,diagnostics=diag))
        except (GitHubError,httpx.HTTPError,ValueError,TypeError,KeyError) as error:
            response_at = _time(clock.now())
            category = _failure(error,github.last_response,github.transport_failed)
            # No transport response => no fabricated observed_at.
            received = response_at if github.last_response is not None else None
            diag = (_diagnostic(category,github.last_response,round((clock.monotonic()-request_tick)*1000)) if received else
                    SafeDiagnostics(category=category))
            rows.append(RepositoryObservation(identity,url,'unavailable',request,received,received,diagnostics=diag))
            if category in {'BUDGET_EXHAUSTED','RATE_LIMIT','AUTHENTICATION_OR_PERMISSION'}:
                stop_category = category
    page = TrendingObservation('unavailable',None,None,None,
                               diagnostics=SafeDiagnostics(category='BUDGET_EXHAUSTED'))
    if can_collect():
        request = _time(clock.now())
        request_tick = clock.monotonic()
        github.last_response = None
        try:
            fetched,digest = github.trending()
            response_at = _time(clock.now())
            duration_ms = round((clock.monotonic()-request_tick)*1000)
            _digest(digest)
            names = set()
            if not fetched or len(fetched) > rules.MAX_TRENDING_ENTRIES:
                raise ObservationValidationError('INVALID_TRENDING_PAGE')
            for expected,(name,rank) in enumerate(fetched,1):
                _name(name)
                if _integer(rank,1) != expected or name.lower() in names:
                    raise ObservationValidationError('INVALID_TRENDING_PAGE')
                names.add(name.lower())
            by_name = {r.metadata.full_name.lower():r.github_id for r in rows if r.metadata}
            trending = tuple(TrendingEntry(name,rank,by_name.get(name.lower()),
                'confirmed' if name.lower() in by_name else 'unresolved_identity')
                for name,rank in fetched[:rules.MAX_TRENDING_ENTRIES])
            page = TrendingObservation('confirmed',request,response_at,response_at,trending,digest,
                diagnostics=_diagnostic(None,github.last_response,duration_ms))
        except (GitHubError,httpx.HTTPError,ValueError,TypeError,KeyError) as error:
            response_at = _time(clock.now())
            category = _failure(error,github.last_response,github.transport_failed)
            received = response_at if github.last_response is not None else None
            page = TrendingObservation('unavailable',request,received,received,
                diagnostics=(_diagnostic(category,github.last_response,round((clock.monotonic()-request_tick)*1000)) if received else
                             SafeDiagnostics(category=category)))
    completed = _time(clock.now())
    success = sum(r.status == 'confirmed' for r in rows)+(page.status == 'confirmed')
    units = len(rows)+1
    eligible = sum(r.status == 'confirmed' and observation_allowed(r.request_started_at,boundary)
                   and observation_allowed(r.observed_at,boundary) for r in rows)
    eligible += (page.status == 'confirmed' and observation_allowed(page.request_started_at,boundary)
                 and observation_allowed(page.observed_at,boundary) and any(e.github_id for e in page.entries))
    payload = ObservationPayload(observation_id or str(uuid4()),
        'fixture-observation' if store.namespace == 'fixture' else 'production-observation',
        store.namespace == 'fixture',boundary-timedelta(days=7),boundary,started,completed,
        producer,PoolReference(stored.reference.object_key,stored.validation.envelope.integrity.payload_sha256),
        tuple(rows),page,'SUCCESS' if success == units else 'PARTIAL_SUCCESS' if success else 'FAILED',
        'FULL' if eligible == units else 'PARTIAL' if eligible else 'NONE')
    envelope = None
    try:
        envelope = seal_observation(payload)
        validate_observation(envelope,now=completed,namespace=store.namespace)
    except ObservationValidationError as error:
        return ObservationJobResult('FAILED',boundary,diagnostics=(error.category,))
    try:
        store.advance_cutoff(completed)
        receipt = store.publish_observation(envelope)
    except (ObservationValidationError,R2ObservationError) as error:
        return ObservationJobResult('FAILED',boundary,envelope=envelope,diagnostics=(error.category,))
    return ObservationJobResult(payload.collection_status if receipt.publication_status == 'VERIFIED' else 'FAILED',
        boundary,envelope,receipt,(() if receipt.publication_status == 'VERIFIED' else ('PUBLICATION_UNCONFIRMED',)),
        _time(clock.now()))
