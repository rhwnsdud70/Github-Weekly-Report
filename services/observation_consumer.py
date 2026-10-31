"""Bounded read-only observation selection; deliberately unconnected to reports."""
from dataclasses import replace
from datetime import timedelta, timezone
import hashlib
import json
import re

from config import observation_rules as rules
from models.domain import Metric, WeeklySnapshot, instant
from models.observation_context import (
    ObservationProvenance, BoundarySnapshot, LegacySnapshot, LatestMetadata,
    ConsumerDiagnostic, SnapshotSelection, SnapshotComparison, TrendingSelection,
    RepositoryObservationContext, BoundaryCoverage, CoverageSummary, ObservationContext,
)
from services.metric_service import growth
from services.observation_validation import (
    _time, _integer, _metadata, _decode, validate_observation, ObservationValidationError,
)
from services.observation_timing import observation_allowed
from services.r2_observation_store import R2ObservationError
from services.scheduler import report_period


class ObservationConsumerError(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__('Observation consumer: '+category)


def _utc(value):
    """Legacy/domain datetimes may be KST; preserve the instant, never adjust it."""
    return instant(value).astimezone(timezone.utc)


def _tie(provenance):
    return (provenance.observed_at,provenance.request_started_at,
            provenance.object_id,provenance.object_key or '')


def legacy_from_weekly_snapshot(snapshot: WeeklySnapshot, *, request_started_at=None,
                                fixture=False) -> LegacySnapshot:
    """Detach a domain Snapshot without opening SQLite or inventing provenance."""
    try:
        if not isinstance(snapshot,WeeklySnapshot) or not isinstance(snapshot.identity,str): raise ValueError()
        match = re.fullmatch(r'id:([1-9][0-9]*)',snapshot.identity)
        if not match: raise ValueError()
        return LegacySnapshot(int(match[1]),_utc(snapshot.period_end),_utc(snapshot.measured_at),
            snapshot.stars,snapshot.forks,_utc(request_started_at) if request_started_at is not None else None,fixture)
    except (ValueError,TypeError,AttributeError):
        raise ObservationConsumerError('INVALID_LEGACY_SNAPSHOT') from None


def _provenance(stored, row):
    return ObservationProvenance(row.source_type,row.source_url,stored.reference.object_key,
        stored.reference.object_id,stored.validation.envelope.integrity.payload_sha256,
        row.request_started_at,row.observed_at)


def _snapshot_selection(rows):
    if not rows: return SnapshotSelection('MISSING')
    # Inspect ALL equal-time groups before earliest selection. Later conflicting
    # observations are not silently hidden behind a convenient earlier value.
    groups = {}
    for row in rows:
        # Same numeric ID's Star/Fork values describe the same GitHub state,
        # including an independently supplied legacy row. Source priority must
        # not conceal disagreement at the exact same observation instant.
        groups.setdefault(row.provenance.observed_at,[]).append(row)
    conflicts = []
    for group in groups.values():
        for field in ('stars','forks'):
            if len({getattr(r,field) for r in group if getattr(r,field) is not None}) > 1:
                conflicts.extend(r.provenance for r in group)
        if len({r.metadata for r in group if r.metadata is not None}) > 1:
            conflicts.extend(r.provenance for r in group)
    if conflicts:
        return SnapshotSelection('CONFLICT',conflict_sources=tuple(sorted(set(conflicts),key=_tie)))
    return SnapshotSelection('CONFIRMED',min(rows,key=lambda r:_tie(r.provenance)))


def _trending_selection(pages, boundary):
    if not pages: return TrendingSelection('MISSING',boundary)
    groups = {}
    for page in pages:
        groups.setdefault(page.provenance.observed_at,[]).append(page)
    for group in groups.values():
        # Whole-page identity includes HTML digest and the validated name/ID/rank
        # mappings; unresolved identities never acquire a rank from another page.
        if len({(p.page_sha256,p.entries) for p in group}) > 1:
            return TrendingSelection('CONFLICT',boundary,
                conflict_sources=tuple(sorted({p.provenance for p in group},key=_tie)))
    return min(pages,key=lambda p:_tie(p.provenance))


def _comparison(current, previous):
    a,b = current.snapshot,previous.snapshot
    def delta(field):
        new,old = getattr(a,field,None),getattr(b,field,None)
        return Metric.known(new-old,'boundary observations') if new is not None and old is not None else Metric()
    return SnapshotComparison(delta('stars'),growth(getattr(a,'stars',None),getattr(b,'stars',None),'boundary observations'),
        delta('forks'),growth(getattr(a,'forks',None),getattr(b,'forks',None),'boundary observations'),
        (a.provenance.observed_at-b.provenance.observed_at).total_seconds() if a and b else None)


def consume_observations(*, store, period, cutoff, repository_ids=(), legacy_snapshots=(),
                         latest_metadata=()) -> ObservationContext:
    """Read current/previous boundary once, freeze provenance and select honestly.

    Inputs are detached immutable DTOs, not a database handle. A fresh report_read
    Store owns request accounting. Any incomplete R2 read invalidates R2-derived
    selections for this context; do not fall back to legacy to disguise access
    failure. Normal empty listings may use independently valid legacy records.
    """
    try:
        cutoff = _time(cutoff)
        start,end = _utc(period.start),_utc(period.end)
        if (end-start != timedelta(hours=168) or report_period(end).end != end or end > cutoff
                or report_period(cutoff).end != end):
            raise ObservationConsumerError('INVALID_REPORT_PERIOD')
        if (store.as_of != cutoff or store.boundary != end or store.profile != 'report_read'
                or store.logical_requests or store.physical_attempts):
            raise ObservationConsumerError('INVALID_STORE_CONTEXT')
        if not isinstance(repository_ids,(tuple,list)) or len(repository_ids) > rules.POOL_LIMIT:
            raise ObservationConsumerError('REPOSITORY_LIMIT')
        ids = {_integer(i,1) for i in repository_ids}
        # Bounded detached inputs; no user-supplied generator can trigger I/O.
        if not isinstance(legacy_snapshots,(tuple,list)) or len(legacy_snapshots) > rules.POOL_LIMIT*2:
            raise ObservationConsumerError('LEGACY_INPUT_LIMIT')
        if not isinstance(latest_metadata,(tuple,list)) or len(latest_metadata) > rules.POOL_LIMIT:
            raise ObservationConsumerError('LATEST_INPUT_LIMIT')
        if len(ids) > rules.POOL_LIMIT:
            raise ObservationConsumerError('REPOSITORY_LIMIT')
        repository_ids = tuple(repository_ids)
        legacy_snapshots = tuple(legacy_snapshots)
        latest_metadata = tuple(latest_metadata)
    except (ValueError,TypeError,AttributeError) as error:
        if isinstance(error,ObservationConsumerError): raise
        raise ObservationConsumerError('INVALID_CONSUMER_INPUT') from None
    diagnostics,coverages,readings,pages = [],[],{start:[],end:[]},[]
    failed = False
    for boundary in (start,end):
        refs,loaded = (),0
        try:
            refs = store.list_observations(boundary)
            if store.as_of != cutoff:
                raise R2ObservationError('read','CONSUMER_CUTOFF_CHANGED')
            if len(refs) > rules.OBJECTS_PER_BOUNDARY:
                raise R2ObservationError('list','OBJECT_LIMIT')
            # The fixed list is never refreshed even if new objects arrive.
            for ref in refs:
                stored = store.get_observation(boundary,ref.object_id)
                if store.as_of != cutoff:
                    raise R2ObservationError('read','CONSUMER_CUTOFF_CHANGED')
                if stored is None: raise R2ObservationError('get','LISTED_OBJECT_MISSING')
                if stored.reference != ref:
                    raise R2ObservationError('get','LIST_GET_REFERENCE_MISMATCH')
                valid = validate_observation(stored.validation.envelope,now=cutoff,namespace=store.namespace)
                p = valid.envelope.payload
                if p.boundary_end != boundary:
                    raise R2ObservationError('get','KEY_PAYLOAD_MISMATCH')
                loaded += 1
                for row in p.repositories:
                    if row.github_id in valid.eligible_repository_ids:
                        readings[boundary].append(BoundarySnapshot(row.github_id,boundary,row.stars,row.forks,
                                                                  row.metadata,_provenance(stored,row)))
                    else:
                        diagnostics.append(ConsumerDiagnostic('SOURCE_INELIGIBLE',boundary,row.github_id,ref.object_key))
                if boundary == end and valid.trending_eligible:
                    pages.append(TrendingSelection('CONFIRMED',boundary,p.trending.entries,
                        p.trending.page_sha256,_provenance(stored,p.trending)))
                for source,reason in valid.ineligible_reasons:
                    if source == 'trending': diagnostics.append(ConsumerDiagnostic('TRENDING_INELIGIBLE',boundary,
                                                                                   object_key=ref.object_key))
            coverages.append(BoundaryCoverage(boundary,'COMPLETE' if refs else 'EMPTY',len(refs),loaded,
                                               len({r.github_id for r in readings[boundary]})))
            if not refs: diagnostics.append(ConsumerDiagnostic('OBSERVATION_MISSING',boundary))
        except (R2ObservationError,ObservationValidationError) as error:
            failed = True
            diagnostics.append(ConsumerDiagnostic(error.category,boundary,http_status=getattr(error,'http_status',None)))
            coverages.append(BoundaryCoverage(boundary,'UNAVAILABLE',len(refs),loaded,0))
    # Do not partially trust an incomplete set: an unread object can conflict or
    # have an earlier response. No DB restore/fallback/side effects are performed.
    if failed:
        readings = {start:[],end:[]}; pages = []
        diagnostics.append(ConsumerDiagnostic('OBSERVATION_STORE_UNAVAILABLE'))
    for row in legacy_snapshots:
        try:
            if not isinstance(row,LegacySnapshot): raise ObservationConsumerError('INVALID_LEGACY_SNAPSHOT')
            identity = _integer(row.github_id,1)
            boundary,observed = _utc(row.boundary),_utc(row.measured_at)
            if type(row.fixture) is not bool or row.fixture != (store.namespace == 'fixture'):
                raise ObservationConsumerError('FIXTURE_NAMESPACE_MISMATCH')
            if boundary not in readings: raise ObservationConsumerError('LEGACY_BOUNDARY_MISMATCH')
            if row.request_started_at is None: raise ObservationConsumerError('LEGACY_REQUEST_TIME_MISSING')
            request = _utc(row.request_started_at)
            if (not request <= observed <= cutoff or not observation_allowed(request,boundary)
                    or not observation_allowed(observed,boundary)):
                raise ObservationConsumerError('LEGACY_OUTSIDE_WINDOW')
            for value in (row.stars,row.forks):
                if value is not None: _integer(value)
            if row.stars is None and row.forks is None: raise ObservationConsumerError('LEGACY_VALUES_MISSING')
            raw = json.dumps([identity,boundary.isoformat(),request.isoformat(),observed.isoformat(),row.stars,row.forks],
                             separators=(',',':')).encode()
            # This digest identifies detached row content, NOT an R2 object SHA.
            digest = hashlib.sha256(raw).hexdigest()
            if not failed:
                readings[boundary].append(BoundarySnapshot(identity,boundary,row.stars,row.forks,None,
                    ObservationProvenance('legacy_sqlite_snapshot','sqlite:weekly_snapshots',
                        None,'legacy-'+digest,None,request,observed,'batch_completion')))
        except (ValueError,TypeError,AttributeError) as error:
            category = error.category if isinstance(error,(ObservationConsumerError,ObservationValidationError)) else 'INVALID_LEGACY_SNAPSHOT'
            diagnostics.append(ConsumerDiagnostic(category))
    latest = {}
    for row in latest_metadata:
        try:
            if not isinstance(row,LatestMetadata): raise ValueError()
            if type(row.fixture) is not bool or row.fixture != (store.namespace == 'fixture'): raise ValueError()
            observed = _time(row.observed_at)
            identity = _integer(row.metadata.github_id,1)
            meta = _metadata(_decode(row.metadata),identity,observed)
            if observed > cutoff or identity in latest: raise ValueError()
            for count in (row.stars,row.forks):
                if count is not None: _integer(count)
            latest[identity] = replace(row,metadata=meta)
        except (ValueError,TypeError,AttributeError):
            raise ObservationConsumerError('INVALID_LATEST_METADATA') from None
    ids.update(r.github_id for group in readings.values() for r in group)
    ids.update(latest)
    # Up to40 per envelope across8 objects may contain distinct IDs. Read budget
    # remains bounded (640 rows); requested IDs only scope output when supplied.
    requested = {_integer(i,1) for i in repository_ids}
    if requested: ids = requested
    trending = TrendingSelection('UNAVAILABLE',end) if failed else _trending_selection(pages,end)
    repositories = []
    for identity in sorted(ids):
        selections = []
        for boundary in (end,start):
            selection = SnapshotSelection('UNAVAILABLE') if failed else _snapshot_selection(
                [r for r in readings[boundary] if r.github_id == identity])
            selections.append(selection)
            if selection.status in {'MISSING','CONFLICT','UNAVAILABLE'}:
                diagnostics.append(ConsumerDiagnostic('OBSERVATION_'+selection.status,boundary,identity))
        rank = next((e.rank for e in trending.entries if e.github_id == identity),None)
        repositories.append(RepositoryObservationContext(identity,*selections,latest.get(identity),
            _comparison(*selections),Metric.known(rank,'stored weekly Trending page') if rank is not None else Metric()))
    if trending.status == 'CONFLICT': diagnostics.append(ConsumerDiagnostic('TRENDING_CONFLICT',end))
    summary = CoverageSummary(len(repositories),sum(r.current.status == 'CONFIRMED' for r in repositories),
        sum(r.previous.status == 'CONFIRMED' for r in repositories),
        sum(r.comparison.star_delta.status == 'confirmed' for r in repositories),
        sum('CONFLICT' in {r.current.status,r.previous.status} for r in repositories),
        sum('UNAVAILABLE' in {r.current.status,r.previous.status} for r in repositories),
        sum('MISSING' in {r.current.status,r.previous.status} for r in repositories),trending.status)
    return ObservationContext(start,end,cutoff,store.namespace,tuple(repositories),trending,tuple(coverages),tuple(diagnostics),summary)
