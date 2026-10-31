"""Strict offline observation schema, provenance, time, and checksum validation.

Late but truthful records remain valid with NONE/PARTIAL boundary coverage.
Validation is not proof that a producer actually contacted GitHub or R2.
"""
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re
from uuid import UUID

from config import observation_rules as rules
from models.observation import (
    Producer, PoolReference, SafeDiagnostics, RepositoryMetadata, RepositoryObservation,
    TrendingEntry, TrendingObservation, ObservationPayload, Integrity, ObservationEnvelope,
    ObservationValidation,
)
from services.observation_timing import observation_allowed
from services.scheduler import KST, report_period


class ObservationValidationError(ValueError):
    """Only fixed categories are exposed; never echo a value, key, or SDK body."""
    def __init__(self, category):
        self.category = category
        super().__init__('Observation validation: '+category)


def _fail(category):
    raise ObservationValidationError(category)


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
        if not isinstance(result,datetime) or result.tzinfo is None or result.utcoffset() != timedelta(0):
            _fail('UTC_TIMESTAMP_REQUIRED')
        return result
    except (TypeError,ValueError,OverflowError,AttributeError) as error:
        if isinstance(error,ObservationValidationError):
            raise
        raise ObservationValidationError('INVALID_TIMESTAMP') from None


TIME_FIELDS = frozenset({'period_start','boundary_end','started_at','completed_at','request_started_at',
                         'response_received_at','observed_at','created_at','pushed_at'})


def _normalize(value, depth=0, key='', counter=None):
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > rules.MAX_JSON_DEPTH or counter[0] > rules.MAX_JSON_NODES:
        _fail('JSON_COMPLEXITY_LIMIT')
    if is_dataclass(value):
        value = {field.name:getattr(value,field.name) for field in fields(value)}
    if key in TIME_FIELDS and value is not None:
        return _time(value).isoformat().replace('+00:00','Z')
    if isinstance(value,dict):
        if any(not isinstance(name,str) for name in value):
            _fail('INVALID_JSON')
        return {name:_normalize(item,depth+1,name,counter) for name,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [_normalize(item,depth+1,'',counter) for item in value]
    if value is None or type(value) in (str,int,bool):
        return value
    _fail('INVALID_JSON_VALUE')  # Floats/NaN/Infinity and arbitrary Python objects are forbidden.


def _json_bytes(value):
    try:
        encoded = json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,
                             allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise ObservationValidationError('INVALID_JSON') from None
    if len(encoded) > rules.MAX_JSON_BYTES:
        _fail('JSON_SIZE_LIMIT')
    return encoded


def canonical_payload(payload):
    """UTC Z normalization precedes the design's sorted compact UTF-8 JSON encoding."""
    return _json_bytes(_normalize(payload))


def payload_sha256(payload):
    return hashlib.sha256(canonical_payload(payload)).hexdigest()


def seal_observation(payload):
    """Construct integrity only. Call validate_observation before trusting a record."""
    return ObservationEnvelope(payload,Integrity(payload_sha256(payload)),rules.SCHEMA_VERSION)


def encode_observation(envelope):
    return _json_bytes(_normalize(envelope))


def _pairs(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            _fail('DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def _decode(value):
    try:
        if isinstance(value,str):
            value = value.encode('utf-8')
        if isinstance(value,bytes):
            if len(value) > rules.MAX_JSON_BYTES:
                _fail('JSON_SIZE_LIMIT')
            value = json.loads(value.decode('utf-8'),object_pairs_hook=_pairs,
                               parse_constant=lambda _: _fail('INVALID_JSON_VALUE'))
        return _normalize(value)
    except (ValueError,TypeError,UnicodeError,RecursionError) as error:
        if isinstance(error,ObservationValidationError):
            raise
        raise ObservationValidationError('INVALID_JSON') from None


def _shape(value, cls):
    if not isinstance(value,dict) or set(value) != {field.name for field in fields(cls)}:
        _fail('SCHEMA_FIELDS_MISMATCH')
    return value


def _integer(value, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _fail('INVALID_INTEGER')
    return value


def _text(value, maximum=rules.MAX_TEXT_LENGTH):
    if not isinstance(value,str) or len(value) > maximum or any(ord(c)<32 and c not in '\n\t' for c in value):
        _fail('INVALID_TEXT')
    return value


def _name(value):
    if (not isinstance(value,str) or not re.fullmatch(rules.REPOSITORY_NAME_PATTERN,value)
            or value.split('/')[-1] in {'.','..'}):
        _fail('INVALID_REPOSITORY_NAME')
    return value


def _digest(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        _fail('INVALID_DIGEST')


def _uuid(value):
    try:
        if not isinstance(value,str) or str(UUID(value)) != value:
            _fail('INVALID_OBSERVATION_ID')
    except (ValueError,TypeError,AttributeError) as error:
        if isinstance(error,ObservationValidationError):
            raise
        raise ObservationValidationError('INVALID_OBSERVATION_ID') from None


def _diagnostics(data, confirmed):
    _shape(data,SafeDiagnostics)
    category = data['category']
    if (confirmed and category is not None) or (not confirmed and category not in rules.FAILURE_CATEGORIES):
        _fail('INVALID_FAILURE_CATEGORY')
    if data['http_status'] is not None:
        _integer(data['http_status'],100,599)
        if confirmed and not 200 <= data['http_status'] < 300:
            _fail('INCONSISTENT_HTTP_STATUS')
    if data['rate_limit_resource'] not in {None,'core','search','graphql'}:
        _fail('INVALID_RATE_LIMIT_RESOURCE')
    for key in ('rate_limit_limit','rate_limit_remaining','rate_limit_reset','retry_after','duration_ms'):
        if data[key] is not None:
            _integer(data[key])
    return SafeDiagnostics(**data)


def _source_times(data, started, completed, boundary, confirmed, diagnostics):
    request, response, observed = (data[key] for key in ('request_started_at','response_received_at','observed_at'))
    if request is None:
        if (confirmed or response is not None or observed is not None or diagnostics.http_status is not None
                or diagnostics.duration_ms is not None):
            _fail('MISSING_SOURCE_TIMESTAMP')
        return (None,None,None),False,'not_observed'
    request = _time(request)
    if not started <= request <= completed:
        _fail('SOURCE_OUTSIDE_JOB')
    if response is None:
        if confirmed or observed is not None or diagnostics.http_status is not None:
            _fail('MISSING_SOURCE_TIMESTAMP')
        return (request,None,None),False,'no_response'
    response, observed = _time(response),_time(observed)
    if not request <= response <= completed or observed != response:
        _fail('REVERSED_OR_ADJUSTED_TIMESTAMP')
    if (diagnostics.duration_ms is None or
            abs((response-request).total_seconds()*1000-diagnostics.duration_ms) > rules.MAX_CLOCK_DURATION_ERROR_MS):
        _fail('CLOCK_DURATION_MISMATCH')
    eligible = confirmed and observation_allowed(request,boundary) and observation_allowed(observed,boundary)
    return (request,response,observed),eligible,('outside_boundary_window' if confirmed else 'source_unavailable')


def _metadata(data, identity, observed):
    _shape(data,RepositoryMetadata)
    if _integer(data['github_id'],1) != identity:
        _fail('REPOSITORY_ID_MISMATCH')
    name = _name(data['full_name'])
    if data['html_url'] != 'https://github.com/'+name:
        _fail('INVALID_SOURCE_URL')
    created = _time(data['created_at'])
    pushed = _time(data['pushed_at']) if data['pushed_at'] is not None else None
    if created > observed or (pushed is not None and not created <= pushed <= observed):
        _fail('FUTURE_METADATA_TIMESTAMP')
    _text(data['description'])
    if data['language'] is not None:
        _text(data['language'],64)
    topics = data['topics']
    if not isinstance(topics,list) or len(topics)>30 or any(not isinstance(t,str) or not re.fullmatch('[a-z0-9-]{1,50}',t) for t in topics):
        _fail('INVALID_TOPICS')
    if type(data['archived']) is not bool or type(data['disabled']) is not bool:
        _fail('INVALID_BOOLEAN')
    return RepositoryMetadata(**{**data,'created_at':created,'pushed_at':pushed,'topics':tuple(topics)})


def validate_observation(value, *, now, namespace='production'):
    """Validate a model/JSON/bytes without I/O. Explicit UTC cutoff keeps tests pure."""
    try:
        return _validate(value,now=now,namespace=namespace)
    except ObservationValidationError:
        raise
    except (ValueError,TypeError,AttributeError,KeyError,OverflowError,RecursionError):
        raise ObservationValidationError('MALFORMED_OBSERVATION') from None


def _validate(value, *, now, namespace):
    now = _time(now)
    data = _decode(value)
    _json_bytes(data)
    _shape(data,ObservationEnvelope)
    if type(data['schema_version']) is not int or data['schema_version'] != rules.SCHEMA_VERSION:
        _fail('UNSUPPORTED_SCHEMA')
    payload = _shape(data['payload'],ObservationPayload)
    integrity = _shape(data['integrity'],Integrity)
    _digest(integrity['payload_sha256'])
    if integrity['algorithm'] != 'sha256':
        _fail('UNSUPPORTED_INTEGRITY')
    if not hmac.compare_digest(integrity['payload_sha256'],payload_sha256(payload)):
        _fail('CHECKSUM_MISMATCH')
    if namespace not in {'production','fixture'} or type(payload['fixture']) is not bool:
        _fail('INVALID_NAMESPACE')
    expected_mode = 'production-observation' if namespace == 'production' else 'fixture-observation'
    if payload['mode'] != expected_mode or payload['fixture'] is not (namespace == 'fixture'):
        _fail('FIXTURE_NAMESPACE_MISMATCH')
    _uuid(payload['observation_id'])
    start,boundary,started,completed = (_time(payload[key]) for key in
                                      ('period_start','boundary_end','started_at','completed_at'))
    if report_period(boundary).end != boundary or boundary-start != timedelta(hours=168):
        _fail('INVALID_REPORT_PERIOD')
    if not started <= completed <= now or boundary > now:
        _fail('FUTURE_OR_REVERSED_JOB_TIME')
    producer = _shape(payload['producer'],Producer)
    _name(producer['repository'])
    if not isinstance(producer['run_id'],str) or not re.fullmatch('[0-9]{1,24}',producer['run_id']):
        _fail('INVALID_PRODUCER')
    _integer(producer['attempt'],1)
    if not isinstance(producer['code_version'],str) or not re.fullmatch('[0-9a-f]{7,64}',producer['code_version']):
        _fail('INVALID_CODE_VERSION')
    pool = _shape(payload['pool_ref'],PoolReference)
    _digest(pool['sha256'])
    prefix = 'observation-pools/v1/production/' if namespace == 'production' else 'test-observation-pools/v1/'
    if not isinstance(pool['key'],str) or not re.fullmatch(re.escape(prefix)+r'\d{4}-\d{2}-\d{2}/[0-9a-f-]{36}\.json',pool['key']):
        _fail('INVALID_POOL_REFERENCE')
    path_date,pool_id = pool['key'][len(prefix):].split('/')
    try:
        pool_date = datetime.strptime(path_date,'%Y-%m-%d').date()
    except ValueError:
        _fail('INVALID_POOL_REFERENCE')
    if pool_date > boundary.astimezone(KST).date():
        _fail('FUTURE_POOL_REFERENCE')
    _uuid(pool_id.removesuffix('.json'))
    rows = payload['repositories']
    if not isinstance(rows,list) or len(rows)>rules.POOL_LIMIT:
        _fail('REPOSITORY_LIMIT')
    repositories, identities, eligible_ids, reasons = [],set(),[],[]
    successes = eligible_units = 0
    for row in rows:
        _shape(row,RepositoryObservation)
        identity = _integer(row['github_id'],1)
        if identity in identities:
            _fail('DUPLICATE_REPOSITORY_ID')
        identities.add(identity)
        if row['source_type'] != 'github_repository_metadata' or row['source_url'] != f'https://api.github.com/repositories/{identity}':
            _fail('INVALID_SOURCE_URL')
        if row['status'] not in {'confirmed','unavailable'}:
            _fail('INVALID_SOURCE_STATUS')
        confirmed = row['status'] == 'confirmed'
        diagnostics = _diagnostics(row['diagnostics'],confirmed)
        times,eligible,reason = _source_times(row,started,completed,boundary,confirmed,diagnostics)
        metadata = None
        if confirmed:
            _integer(row['stars']); _integer(row['forks'])
            metadata = _metadata(row['metadata'],identity,times[2])
        elif row['metadata'] is not None or row['stars'] is not None or row['forks'] is not None:
            _fail('UNAVAILABLE_HAS_VALUES')
        repositories.append(RepositoryObservation(identity,row['source_url'],row['status'],*times,
            metadata,row['stars'],row['forks'],row['source_type'],diagnostics))
        successes += confirmed
        if eligible:
            eligible_ids.append(identity); eligible_units += 1
        else:
            reasons.append((str(identity),reason))
    page = _shape(payload['trending'],TrendingObservation)
    if page['source_type'] != 'github_trending_weekly' or page['source_url'] != 'https://github.com/trending?since=weekly':
        _fail('INVALID_SOURCE_URL')
    if page['status'] not in {'confirmed','unavailable'}:
        _fail('INVALID_SOURCE_STATUS')
    confirmed = page['status'] == 'confirmed'
    diagnostics = _diagnostics(page['diagnostics'],confirmed)
    times,page_eligible,reason = _source_times(page,started,completed,boundary,confirmed,diagnostics)
    if not isinstance(page['entries'],list) or len(page['entries'])>rules.MAX_TRENDING_ENTRIES:
        _fail('TRENDING_LIMIT')
    if confirmed:
        _digest(page['page_sha256'])
        if not page['entries']:
            _fail('EMPTY_TRENDING_PAGE')
    elif page['entries'] or page['page_sha256'] is not None:
        _fail('UNAVAILABLE_HAS_VALUES')
    entries, names, resolved = [],set(),set()
    metadata_by_id = {r.github_id:r.metadata for r in repositories if r.metadata is not None}
    for rank,entry in enumerate(page['entries'],1):
        _shape(entry,TrendingEntry)
        name = _name(entry['full_name'])
        if name.lower() in names or _integer(entry['rank'],1,rules.MAX_TRENDING_ENTRIES) != rank:
            _fail('DUPLICATE_OR_INVALID_RANK')
        names.add(name.lower())
        identity = entry['github_id']
        if identity is None:
            if entry['status'] != 'unresolved_identity':
                _fail('UNRESOLVED_IDENTITY')
        else:
            _integer(identity,1)
            if entry['status'] != 'confirmed' or identity in resolved or identity not in metadata_by_id or metadata_by_id[identity].full_name.lower() != name.lower():
                _fail('TRENDING_IDENTITY_MISMATCH')
            resolved.add(identity)
        entries.append(TrendingEntry(name,rank,identity,entry['status']))
    page_eligible = page_eligible and bool(resolved)
    if not page_eligible:
        reasons.append(('trending',reason if not confirmed else 'outside_window_or_unresolved_identity'))
    successes += confirmed
    eligible_units += page_eligible
    units = len(rows)+1
    expected_status = 'SUCCESS' if successes == units else 'PARTIAL_SUCCESS' if successes else 'FAILED'
    expected_coverage = 'FULL' if eligible_units == units else 'PARTIAL' if eligible_units else 'NONE'
    if payload['collection_status'] != expected_status or payload['boundary_coverage'] != expected_coverage:
        _fail('INCONSISTENT_COLLECTION_OR_COVERAGE')
    if payload['publication_status'] not in {'VERIFIED','UNCONFIRMED','FAILED'}:
        _fail('INVALID_PUBLICATION_STATUS')
    typed = ObservationPayload(payload['observation_id'],payload['mode'],payload['fixture'],start,boundary,
        started,completed,Producer(**producer),PoolReference(**pool),tuple(repositories),
        TrendingObservation(page['status'],*times,tuple(entries),page['page_sha256'],page['source_type'],page['source_url'],diagnostics),
        expected_status,expected_coverage,payload['publication_status'])
    return ObservationValidation(ObservationEnvelope(typed,Integrity(integrity['payload_sha256']),rules.SCHEMA_VERSION),
                                 tuple(sorted(eligible_ids)),bool(page_eligible),tuple(reasons))
