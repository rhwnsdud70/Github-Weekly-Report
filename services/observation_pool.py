"""Pure, deterministic observation pool selection. No database or network adapters."""
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import re

from config import observation_rules as rules
from models.domain import instant
from services.scheduler import report_period


class PoolValidationError(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__('Observation pool: '+category)


@dataclass(frozen=True)
class PoolRecord:
    """Verified adapter input; past signal references affect admission only."""
    github_id: int | None
    full_name: str
    first_seen_at: datetime
    last_seen_at: datetime
    source_period_end: datetime
    source_kind: str = 'discovery'
    verified: bool = False
    fixture: bool = False
    relevant: bool = True
    excluded: bool = False
    meaningful_at: datetime | None = None
    critical_at: datetime | None = None
    watchlisted_at: datetime | None = None
    reported_at: datetime | None = None
    last_observed_at: datetime | None = None
    retry_since: datetime | None = None
    confirmed_missing_count: int = 0
    independent_signals: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    source_run_status: str = 'SUCCESS'


@dataclass(frozen=True)
class PoolEntry:
    record: PoolRecord
    priority: int
    deprioritized: bool
    expires_at: datetime
    admission_reasons: tuple[str, ...]
    source_kinds: tuple[str, ...]


@dataclass(frozen=True)
class PoolResult:
    entries: tuple[PoolEntry, ...]
    diagnostics: tuple[tuple[str, int], ...]
    status: str = 'READY'
    fixture: bool = False
    as_of: datetime | None = None


def _utc(value):
    try:
        return instant(value).astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise PoolValidationError('INVALID_TIMESTAMP') from None


def _normalize(record, as_of, fixture):
    if not isinstance(record,PoolRecord):
        raise PoolValidationError('INVALID_RECORD')
    if type(record.fixture) is not bool or record.fixture != fixture:
        raise PoolValidationError('FIXTURE_MODE_MISMATCH')
    if record.source_kind not in {'discovery','watchlist','top5','event','pool'}:
        raise PoolValidationError('INVALID_SOURCE_KIND')
    if record.source_run_status not in {'SUCCESS','PARTIAL_SUCCESS','FAILED','RUNNING'}:
        raise PoolValidationError('INVALID_RUN_STATUS')
    for value in (record.verified,record.relevant,record.excluded):
        if type(value) is not bool:
            raise PoolValidationError('INVALID_RECORD')
    if (not isinstance(record.full_name,str) or not re.fullmatch(rules.REPOSITORY_NAME_PATTERN,record.full_name)
            or record.full_name.split('/')[-1] in {'.','..'}):
        raise PoolValidationError('INVALID_REPOSITORY_NAME')
    if record.github_id is not None and (type(record.github_id) is not int or record.github_id <= 0):
        raise PoolValidationError('INVALID_REPOSITORY_ID')
    if type(record.confirmed_missing_count) is not int or record.confirmed_missing_count < 0:
        raise PoolValidationError('INVALID_MISSING_COUNT')
    timestamps = {}
    for name in ('first_seen_at','last_seen_at','source_period_end','meaningful_at','critical_at',
                 'watchlisted_at','reported_at','last_observed_at','retry_since'):
        value = getattr(record,name)
        if value is None and name not in {'first_seen_at','last_seen_at','source_period_end'}:
            continue
        timestamps[name] = _utc(value)
        if timestamps[name] > as_of:
            raise PoolValidationError('FUTURE_DATA')
    if timestamps['first_seen_at'] > timestamps['last_seen_at']:
        raise PoolValidationError('REVERSED_TIMESTAMP')
    if report_period(timestamps['source_period_end']).end != timestamps['source_period_end']:
        raise PoolValidationError('INVALID_SOURCE_PERIOD')
    if (not isinstance(record.independent_signals,tuple) or
            any(value not in {'star','trending','activity','discussion','release','security','policy'}
                for value in record.independent_signals)):
        raise PoolValidationError('INVALID_SIGNAL')
    if not isinstance(record.event_refs,tuple) or any(not isinstance(value,str) or len(value)>2048 or
            any(part in {'.','..'} for part in value.split('/')) or not re.fullmatch(
            r'https://github\.com/(?:advisories/GHSA-(?:[a-z0-9]{4}-){2}[a-z0-9]{4}|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull|releases|security|discussions)/[A-Za-z0-9_./-]+)',value)
            for value in record.event_refs):
        raise PoolValidationError('INVALID_EVENT_REFERENCE')
    if record.critical_at is not None and ('security' not in record.independent_signals or not any(re.fullmatch(
            r'https://github\.com/(?:advisories|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/security/advisories)/GHSA-(?:[a-z0-9]{4}-){2}[a-z0-9]{4}',
            value) for value in record.event_refs)):
        raise PoolValidationError('UNSUPPORTED_CRITICAL_REFERENCE')
    return replace(record,**timestamps,independent_signals=tuple(sorted(set(record.independent_signals))),
                   event_refs=tuple(sorted(set(record.event_refs))))


def build_pool(records, *, as_of, fixture=False):
    """Select up to 40 IDs. Malformed/future/mixed-provenance inputs fail closed.

    Unverified/irrelevant/expired/unknown-ID entries are skipped with safe counts.
    TTL endpoints are inclusive; observation alone never renews meaningful TTL.
    """
    try:
        return _build_pool(records,as_of=as_of,fixture=fixture)
    except PoolValidationError:
        raise
    except (TypeError,ValueError,AttributeError,KeyError,OverflowError):
        raise PoolValidationError('MALFORMED_RECORD') from None


def _build_pool(records, *, as_of, fixture):
    if type(fixture) is not bool:
        raise PoolValidationError('INVALID_MODE')
    as_of = _utc(as_of)
    boundary = report_period(as_of).end
    accepted_periods = {boundary-timedelta(weeks=i) for i in range(rules.COMPLETED_WEEKS)}
    groups, counts, origins = {}, Counter(), {}
    for raw in records:
        if isinstance(raw,PoolEntry):
            if not isinstance(raw.source_kinds,tuple) or any(kind not in {
                    'discovery','watchlist','top5','event','pool'} for kind in raw.source_kinds):
                raise PoolValidationError('INVALID_SOURCE_KIND')
            origins.setdefault(raw.record.github_id,set()).update(raw.source_kinds)
            raw = replace(_normalize(raw.record,as_of,fixture),source_kind='pool')
        record = _normalize(raw,as_of,fixture)
        if record.github_id is None:
            counts['unresolved_identity'] += 1
        elif not record.verified or record.source_run_status == 'RUNNING':
            counts['unverified'] += 1
        elif record.source_period_end not in accepted_periods:
            counts['outside_lookback'] += 1
        else:
            groups.setdefault(record.github_id,[]).append(record)
    entries = []
    for identity, versions in groups.items():
        # Latest confirmed metadata wins; lexical tie-break makes input order irrelevant.
        latest = max(versions,key=lambda r:(r.last_seen_at,r.full_name,r.source_kind,repr(r)))
        merged = {}
        for name in ('meaningful_at','critical_at','watchlisted_at','reported_at','last_observed_at','retry_since'):
            values = [getattr(r,name) for r in versions if getattr(r,name) is not None]
            merged[name] = (min(values) if name == 'retry_since' else max(values)) if values else None
        record = replace(latest,**merged,first_seen_at=min(r.first_seen_at for r in versions),
            independent_signals=tuple(sorted({v for r in versions for v in r.independent_signals})),
            event_refs=tuple(sorted({v for r in versions for v in r.event_refs})))
        if not record.relevant or (record.excluded and (record.critical_at is None or
                as_of-record.critical_at > timedelta(days=rules.CRITICAL_ACTIVE_DAYS))):
            counts['irrelevant'] += 1
            continue
        if record.confirmed_missing_count >= rules.MISSING_STOP_COUNT:
            counts['confirmed_missing'] += 1
            continue
        anchors = [v for v in (record.meaningful_at,record.critical_at,record.watchlisted_at,record.reported_at) if v]
        anchor = max(anchors) if anchors else record.first_seen_at
        expires = anchor+timedelta(days=rules.MEANINGFUL_TTL_DAYS if anchors else rules.DISCOVERY_TTL_DAYS)
        if as_of > expires:
            counts['expired'] += 1
            continue
        recent = lambda value,days:value is not None and as_of-value <= timedelta(days=days)
        tier = (0 if recent(record.critical_at,rules.CRITICAL_ACTIVE_DAYS) else
                1 if recent(record.watchlisted_at,rules.RECENT_WATCHLIST_DAYS) else
                2 if recent(record.reported_at,rules.RECENT_TOP_DAYS) else
                3 if record.meaningful_at and (record.independent_signals or record.event_refs) else
                4 if anchors else 5)
        stale = as_of-anchor >= timedelta(days=rules.DEPRIORITIZE_DAYS)
        if stale and tier == 3:
            tier = 4
        reasons = tuple(name for name,value in (
            ('verified_critical',record.critical_at),('watchlist',record.watchlisted_at),
            ('previous_report',record.reported_at),('verified_signal',record.meaningful_at)) if value)
        entries.append(PoolEntry(record,tier,stale,expires,reasons or ('new_exploration',),
                                 tuple(sorted({r.source_kind for r in versions}|origins.get(identity,set())))))
        counts['duplicates_merged'] += len(versions)-1
    if sum(entry.priority == 0 for entry in entries) > rules.POOL_LIMIT:
        counts['CRITICAL_POOL_OVERFLOW'] += 1
        return PoolResult((),tuple(sorted(counts.items())),'CRITICAL_POOL_OVERFLOW',fixture,as_of)
    epoch = datetime(1970,1,1,tzinfo=timezone.utc)
    def order(entry):
        r = entry.record
        meaningful = max(v for v in (r.first_seen_at,r.meaningful_at,r.critical_at,r.watchlisted_at,r.reported_at) if v)
        waiting = min(v for v in (r.last_observed_at or datetime.min.replace(tzinfo=timezone.utc),
                                  r.retry_since or r.last_observed_at or datetime.min.replace(tzinfo=timezone.utc)))
        return entry.priority,entry.deprioritized,-(meaningful-epoch).total_seconds(),waiting,r.github_id
    selected, exploration = [], 0
    for entry in sorted(entries,key=order):
        if len(selected) >= rules.POOL_LIMIT:
            counts['capacity_excluded'] += 1
        elif entry.priority == 5 and exploration >= rules.EXPLORATION_LIMIT:
            counts['exploration_limit'] += 1
        else:
            selected.append(entry)
            exploration += entry.priority == 5
    return PoolResult(tuple(selected),tuple(sorted(counts.items())),fixture=fixture,as_of=as_of)


def pool_manifest(result, *, as_of, fixture=False):
    """Return a detached JSON-ready proposal only; never publishes it."""
    if not isinstance(result,PoolResult):
        raise PoolValidationError('INVALID_RESULT')
    if type(fixture) is not bool or result.fixture != fixture or any(entry.record.fixture != fixture for entry in result.entries):
        raise PoolValidationError('FIXTURE_MODE_MISMATCH')
    if _utc(as_of) != result.as_of:
        raise PoolValidationError('MANIFEST_AS_OF_MISMATCH')
    def encode(value):
        if isinstance(value,datetime):
            return value.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        if isinstance(value,dict):
            return {k:encode(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):
            return [encode(v) for v in value]
        return value
    return encode({'schema_version':rules.SCHEMA_VERSION,'mode':'fixture-pool' if fixture else 'production-pool',
                   'fixture':fixture,'as_of':_utc(as_of),'status':result.status,
                   'entries':[asdict(entry) for entry in result.entries],'diagnostics':dict(result.diagnostics)})
