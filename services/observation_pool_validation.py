"""Pool envelope contract over Phase 1 proposals, without any storage access."""
from dataclasses import replace
from datetime import timedelta, timezone
import hashlib
import hmac
import re

from config import observation_rules as rules
from models.observation import Integrity, Producer
from models.observation_storage import PoolPayload, PoolEnvelope, PoolValidation
from services.observation_pool import PoolEntry, PoolRecord, PoolValidationError, build_pool, pool_manifest
from services.observation_validation import (
    ObservationValidationError, _decode, _shape, _time, _uuid, _digest, _integer, _name, _json_bytes,
)
from services.scheduler import report_period


POOL_TIMES = frozenset({'as_of','expires_at','first_seen_at','last_seen_at','source_period_end',
    'meaningful_at','critical_at','watchlisted_at','reported_at','last_observed_at','retry_since'})
DIAGNOSTICS = frozenset({'unresolved_identity','unverified','outside_lookback','irrelevant',
    'confirmed_missing','expired','duplicates_merged','CRITICAL_POOL_OVERFLOW','capacity_excluded','exploration_limit'})
SOURCE_KINDS = frozenset({'discovery','watchlist','top5','event','pool'})


def _fail(category):
    raise ObservationValidationError(category)


def _pool_normalize(value, key=''):
    if key in POOL_TIMES and value is not None:
        return _time(value).isoformat().replace('+00:00','Z')
    if isinstance(value,dict):
        return {k:_pool_normalize(v,k) for k,v in value.items()}
    if isinstance(value,list):
        return [_pool_normalize(v) for v in value]
    return value


def canonical_pool_payload(payload):
    return _json_bytes(_pool_normalize(_decode(payload)))


def pool_payload_sha256(payload):
    return hashlib.sha256(canonical_pool_payload(payload)).hexdigest()


def seal_pool(result, *, pool_id, created_at, producer):
    """Wrap a Phase 1 result; validation is still mandatory before publication."""
    manifest = pool_manifest(result,as_of=result.as_of,fixture=result.fixture)
    boundary = report_period(result.as_of).end.astimezone(timezone.utc)
    payload = PoolPayload(pool_id,manifest['mode'],result.fixture,boundary-timedelta(days=7),
                          boundary,created_at,producer,manifest)
    return PoolEnvelope(payload,Integrity(pool_payload_sha256(payload)),rules.SCHEMA_VERSION)


def encode_pool(envelope):
    return _json_bytes(_pool_normalize(_decode(envelope)))


def validate_pool(value, *, now, namespace='production'):
    try:
        return _validate(value,now=_time(now),namespace=namespace)
    except ObservationValidationError:
        raise
    except PoolValidationError:
        raise ObservationValidationError('POOL_BUILDER_VALIDATION_FAILED') from None
    except (TypeError,ValueError,KeyError,AttributeError,OverflowError,RecursionError):
        raise ObservationValidationError('MALFORMED_POOL') from None


def _validate(value, *, now, namespace):
    data = _pool_normalize(_decode(value))
    _json_bytes(data)
    _shape(data,PoolEnvelope)
    if type(data['schema_version']) is not int or data['schema_version'] != rules.SCHEMA_VERSION:
        _fail('UNSUPPORTED_SCHEMA')
    payload = _shape(data['payload'],PoolPayload)
    integrity = _shape(data['integrity'],Integrity)
    _digest(integrity['payload_sha256'])
    if integrity['algorithm'] != 'sha256':
        _fail('UNSUPPORTED_INTEGRITY')
    if not hmac.compare_digest(integrity['payload_sha256'],pool_payload_sha256(payload)):
        _fail('CHECKSUM_MISMATCH')
    if namespace not in {'production','fixture'} or type(payload['fixture']) is not bool:
        _fail('INVALID_NAMESPACE')
    mode = 'fixture-pool' if namespace == 'fixture' else 'production-pool'
    if payload['mode'] != mode or payload['fixture'] is not (namespace == 'fixture'):
        _fail('FIXTURE_NAMESPACE_MISMATCH')
    _uuid(payload['pool_id'])
    start,boundary,created = (_time(payload[k]) for k in ('period_start','boundary_end','created_at'))
    if report_period(boundary).end != boundary or boundary-start != timedelta(days=7):
        _fail('INVALID_REPORT_PERIOD')
    if boundary > created or created > now:
        _fail('FUTURE_OR_REVERSED_POOL_TIME')
    producer = _shape(payload['producer'],Producer)
    _name(producer['repository'])
    if not isinstance(producer['run_id'],str) or not re.fullmatch('[0-9]{1,24}',producer['run_id']):
        _fail('INVALID_PRODUCER')
    _integer(producer['attempt'],1)
    if not isinstance(producer['code_version'],str) or not re.fullmatch('[0-9a-f]{7,64}',producer['code_version']):
        _fail('INVALID_CODE_VERSION')
    manifest = payload['manifest']
    if not isinstance(manifest,dict) or set(manifest) != {
            'schema_version','mode','fixture','as_of','status','entries','diagnostics'}:
        _fail('SCHEMA_FIELDS_MISMATCH')
    if (type(manifest['schema_version']) is not int or manifest['schema_version'] != rules.SCHEMA_VERSION or
            manifest['mode'] != mode or type(manifest['fixture']) is not bool or manifest['fixture'] != payload['fixture']):
        _fail('FIXTURE_NAMESPACE_MISMATCH')
    as_of = _time(manifest['as_of'])
    if not boundary <= as_of <= created or report_period(as_of).end != boundary:
        _fail('INVALID_POOL_AS_OF')
    if manifest['status'] not in {'READY','CRITICAL_POOL_OVERFLOW'}:
        _fail('INVALID_POOL_STATUS')
    diagnostics = manifest['diagnostics']
    if not isinstance(diagnostics,dict) or set(diagnostics)-DIAGNOSTICS:
        _fail('INVALID_POOL_DIAGNOSTICS')
    for count in diagnostics.values():
        _integer(count)
    entries = manifest['entries']
    if not isinstance(entries,list) or len(entries)>rules.POOL_LIMIT:
        _fail('REPOSITORY_LIMIT')
    records, typed_entries, ids = [],[],set()
    for entry in entries:
        _shape(entry,PoolEntry)
        row = dict(_shape(entry['record'],PoolRecord))
        identity = _integer(row['github_id'],1)
        if identity in ids:
            _fail('DUPLICATE_REPOSITORY_ID')
        ids.add(identity)
        for key in POOL_TIMES & row.keys():
            if row[key] is not None:
                row[key] = _time(row[key])
        for key in ('independent_signals','event_refs'):
            if not isinstance(row[key],list):
                _fail('SCHEMA_FIELDS_MISMATCH')
            row[key] = tuple(row[key])
        record = PoolRecord(**row)
        _integer(entry['priority'],0,5)
        if type(entry['deprioritized']) is not bool:
            _fail('INVALID_BOOLEAN')
        kinds = entry['source_kinds']
        if (not isinstance(kinds,list) or not kinds or any(not isinstance(k,str) or k not in SOURCE_KINDS for k in kinds)
                or len(set(kinds)) != len(kinds) or record.source_kind not in kinds):
            _fail('INVALID_POOL_SOURCE')
        if not isinstance(entry['admission_reasons'],list):
            _fail('SCHEMA_FIELDS_MISMATCH')
        typed_entries.append(PoolEntry(record,entry['priority'],entry['deprioritized'],_time(entry['expires_at']),
                                       tuple(entry['admission_reasons']),tuple(kinds)))
        records.append(record)
    rebuilt = build_pool(records,as_of=as_of,fixture=payload['fixture'])
    if [e.record.github_id for e in rebuilt.entries] != [e.record.github_id for e in typed_entries]:
        _fail('POOL_BUILDER_MISMATCH')
    for actual, expected in zip(typed_entries,rebuilt.entries):
        if actual != replace(expected,source_kinds=actual.source_kinds):
            _fail('POOL_BUILDER_MISMATCH')
    if manifest['status'] == 'CRITICAL_POOL_OVERFLOW' and (entries or diagnostics.get('CRITICAL_POOL_OVERFLOW',0)<1):
        _fail('INVALID_POOL_STATUS')
    if manifest['status'] == 'READY' and diagnostics.get('CRITICAL_POOL_OVERFLOW',0):
        _fail('INVALID_POOL_STATUS')
    if payload['publication_status'] not in {'UNCONFIRMED','VERIFIED','FAILED'}:
        _fail('INVALID_PUBLICATION_STATUS')
    result = replace(rebuilt,entries=tuple(typed_entries),status=manifest['status'],
                     diagnostics=tuple(sorted(diagnostics.items())))
    typed = PoolPayload(payload['pool_id'],mode,payload['fixture'],start,boundary,created,
                        Producer(**producer),manifest,payload['publication_status'])
    return PoolValidation(PoolEnvelope(typed,Integrity(integrity['payload_sha256']),rules.SCHEMA_VERSION),result)
