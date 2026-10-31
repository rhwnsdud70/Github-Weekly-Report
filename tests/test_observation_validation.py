from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from models.observation import (
    Producer, PoolReference, SafeDiagnostics, RepositoryMetadata, RepositoryObservation,
    TrendingEntry, TrendingObservation, ObservationPayload,
)
from services.observation_validation import (
    ObservationValidationError, seal_observation, encode_observation, validate_observation, payload_sha256,
)


B = datetime(2026,11,1,10,tzinfo=timezone.utc)
NOW = B+timedelta(days=1)
OBS_ID = '00000000-0000-4000-8000-000000000001'


def payload(delay=600, *, fixture=True):
    request = B+timedelta(seconds=max(0,delay-1))
    response = B+timedelta(seconds=delay)
    diagnostic = SafeDiagnostics(http_status=200,duration_ms=int((response-request).total_seconds()*1000))
    metadata = RepositoryMetadata(900001,'synthetic-fixture/repo','https://github.com/synthetic-fixture/repo',
                                  B-timedelta(days=100),description='Synthetic fixture')
    repo = RepositoryObservation(900001,'https://api.github.com/repositories/900001','confirmed',
                                 request,response,response,metadata,1200,80,diagnostics=diagnostic)
    page = TrendingObservation('confirmed',request,response,response,
        (TrendingEntry('synthetic-fixture/repo',1,900001,'confirmed'),),'a'*64,diagnostics=diagnostic)
    return ObservationPayload(OBS_ID,'fixture-observation' if fixture else 'production-observation',fixture,
        B-timedelta(days=7),B,request,response+timedelta(seconds=1),
        Producer('fixture/weekly','123',1,'a'*40),
        PoolReference(('test-observation-pools/v1/' if fixture else 'observation-pools/v1/production/')+
                      '2026-11-01/'+OBS_ID+'.json','b'*64),
        (repo,),page,'SUCCESS','FULL' if delay<=3600 else 'NONE')


def validate(value, **changes):
    return validate_observation(seal_observation(value),now=NOW,namespace='fixture',**changes)


def raw(value=None):
    return json.loads(encode_observation(seal_observation(value or payload())))


def reseal(data):
    data['integrity']['payload_sha256'] = payload_sha256(data['payload'])
    return data


@pytest.mark.parametrize('delay,expected',[(0,'FULL'),(600,'FULL'),(3600,'FULL'),(3601,'NONE'),(14565,'NONE')])
def test_boundary_window_is_separate_from_collection_success(delay,expected):
    result = validate(payload(delay))
    assert result.envelope.payload.collection_status == 'SUCCESS'
    assert result.envelope.payload.boundary_coverage == expected
    assert result.eligible_repository_ids == ((900001,) if expected == 'FULL' else ())
    assert result.trending_eligible is (expected == 'FULL')
    assert result.envelope.payload.repositories[0].observed_at == B+timedelta(seconds=delay)


def test_json_roundtrip_detached_models_and_canonical_checksum():
    data = raw()
    value = validate_observation(json.dumps(data,indent=4),now=NOW,namespace='fixture')
    reverse = dict(reversed(list(data['payload'].items())))
    assert payload_sha256(reverse) == payload_sha256(data['payload'])
    data['payload']['completed_at'] = data['payload']['completed_at'].replace('Z','+00:00')
    assert validate_observation(data,now=NOW,namespace='fixture') == value
    data['payload']['repositories'][0]['stars'] = 999
    assert value.envelope.payload.repositories[0].stars == 1200
    assert value.envelope.payload.producer.run_id == '123'


@pytest.mark.parametrize('field,value',[
    ('stars',True),('stars',-1),('forks',False),('github_id',True),('github_id',0),
    ('source_url','https://secret@example.com/repositories/900001'),
    ('source_url','https://api.github.com/repositories/900001?token=sensitive'),
    ('source_type','other'),
])
def test_invalid_repo_fields_safe_failure(field,value):
    data = raw()
    data['payload']['repositories'][0][field] = value
    with pytest.raises(ObservationValidationError) as caught:
        validate_observation(reseal(data),now=NOW,namespace='fixture')
    assert 'sensitive' not in str(caught.value) and 'secret@' not in str(caught.value)


def test_source_request_cannot_cross_window_or_be_adjusted():
    data = raw(payload(3601))
    result = validate_observation(data,now=NOW,namespace='fixture')
    assert not result.eligible_repository_ids
    data['payload']['repositories'][0]['observed_at'] = (B+timedelta(seconds=10)).isoformat()
    with pytest.raises(ObservationValidationError,match='REVERSED_OR_ADJUSTED_TIMESTAMP'):
        validate_observation(reseal(data),now=NOW,namespace='fixture')


@pytest.mark.parametrize('field,value',[
    ('request_started_at',(B+timedelta(hours=1)).isoformat()),
    ('response_received_at',(B-timedelta(seconds=1)).isoformat()),
    ('observed_at','2026-11-01T10:10:00'),
    ('observed_at','2026-11-01T19:10:00+09:00'),
])
def test_reversed_naive_non_utc_times_rejected(field,value):
    data = raw()
    data['payload']['repositories'][0][field] = value
    with pytest.raises(ObservationValidationError):
        validate_observation(reseal(data),now=NOW,namespace='fixture')


def test_future_job_and_invalid_report_period_rejected():
    with pytest.raises(ObservationValidationError,match='FUTURE_OR_REVERSED_JOB_TIME'):
        validate_observation(seal_observation(payload()),now=B,namespace='fixture')
    with pytest.raises(ObservationValidationError,match='INVALID_REPORT_PERIOD'):
        validate(replace(payload(),boundary_end=B+timedelta(minutes=7)))
    with pytest.raises(ObservationValidationError):
        validate(replace(payload(),period_start=B-timedelta(days=6)))


def test_new_repository_does_not_require_previous_snapshot():
    p = payload()
    repo = replace(p.repositories[0],metadata=replace(p.repositories[0].metadata,created_at=B-timedelta(minutes=1)))
    assert validate(replace(p,repositories=(repo,))).eligible_repository_ids == (900001,)
    assert not hasattr(p,'previous_snapshot')  # Comparison belongs to Phase 5, never fabricated here.


def test_repository_id_duplication_and_limits():
    p = payload()
    with pytest.raises(ObservationValidationError,match='DUPLICATE_REPOSITORY_ID'):
        validate(replace(p,repositories=p.repositories*2))
    with pytest.raises(ObservationValidationError,match='REPOSITORY_LIMIT'):
        validate(replace(p,repositories=p.repositories*41))
    with pytest.raises(ObservationValidationError,match='TRENDING_LIMIT'):
        validate(replace(p,trending=replace(p.trending,entries=p.trending.entries*31)))


def test_exactly_forty_repositories_and_thirty_trending_entries_are_supported():
    p = payload()
    repositories,entries = [],[]
    for identity in range(1,41):
        name = f'fixture/repo{identity}'
        repositories.append(replace(p.repositories[0],github_id=identity,
            source_url=f'https://api.github.com/repositories/{identity}',
            metadata=replace(p.repositories[0].metadata,github_id=identity,full_name=name,
                             html_url='https://github.com/'+name)))
        if identity<=30:
            entries.append(TrendingEntry(name,identity,identity,'confirmed'))
    result = validate(replace(p,repositories=tuple(repositories),trending=replace(p.trending,entries=tuple(entries))))
    assert len(result.eligible_repository_ids) == 40 and result.trending_eligible


def test_integrity_mismatch_and_schema_version():
    data = raw()
    data['payload']['repositories'][0]['stars'] += 1
    with pytest.raises(ObservationValidationError,match='CHECKSUM_MISMATCH'):
        validate_observation(data,now=NOW,namespace='fixture')
    for version in (True,2,0):
        data = raw()
        data['schema_version'] = version
        with pytest.raises(ObservationValidationError,match='UNSUPPORTED_SCHEMA'):
            validate_observation(data,now=NOW,namespace='fixture')


@pytest.mark.parametrize('text',[
    '{"schema_version":1,"schema_version":1}',
    '{"payload":{"stars":1,"stars":2}}',
    '{"stars":NaN}','{"stars":Infinity}','{"stars":-Infinity}',
    'not-json',b'\xff',
])
def test_json_duplicate_keys_and_nonfinite_or_corrupt_data(text):
    with pytest.raises(ObservationValidationError):
        validate_observation(text,now=NOW,namespace='fixture')


def test_size_limit_and_malformed_source_url():
    with pytest.raises(ObservationValidationError,match='JSON_SIZE_LIMIT'):
        validate_observation(b' '*(1024*1024+1),now=NOW,namespace='fixture')
    p = payload()
    with pytest.raises(ObservationValidationError,match='INVALID_SOURCE_URL'):
        validate(replace(p,trending=replace(p.trending,source_url='https://github.com/trending?sig=sensitive')))


def test_fixture_mode_and_pool_namespace_must_agree():
    with pytest.raises(ObservationValidationError,match='FIXTURE_NAMESPACE_MISMATCH'):
        validate_observation(seal_observation(payload()),now=NOW)
    p = payload(fixture=False)
    assert validate_observation(seal_observation(p),now=NOW).eligible_repository_ids == (900001,)
    with pytest.raises(ObservationValidationError,match='INVALID_POOL_REFERENCE'):
        validate_observation(seal_observation(replace(p,pool_ref=payload().pool_ref)),now=NOW)


def test_source_failure_partial_coverage_and_unknown_are_preserved():
    p = payload()
    page = TrendingObservation('unavailable',None,None,None,
                               diagnostics=SafeDiagnostics(category='SOURCE_UNAVAILABLE'))
    result = validate(replace(p,trending=page,collection_status='PARTIAL_SUCCESS',boundary_coverage='PARTIAL'))
    assert result.envelope.payload.trending.observed_at is None
    assert not result.trending_eligible
    repo = RepositoryObservation(900001,p.repositories[0].source_url,'unavailable',None,None,None,
                                 diagnostics=SafeDiagnostics(category='BUDGET_EXHAUSTED'))
    failed = validate(replace(p,repositories=(repo,),trending=page,collection_status='FAILED',boundary_coverage='NONE'))
    assert failed.envelope.payload.repositories[0].stars is None
    assert failed.envelope.payload.repositories[0].metadata is None


def test_unknown_fields_and_raw_error_messages_are_rejected_without_echo():
    data = raw()
    data['payload']['repositories'][0]['diagnostics']['error_message'] = 'sensitive-token-value'
    with pytest.raises(ObservationValidationError) as caught:
        validate_observation(reseal(data),now=NOW,namespace='fixture')
    assert 'sensitive-token-value' not in str(caught.value)


def test_unresolved_or_conflicting_trending_identity_is_not_a_signal():
    p = payload()
    entry = replace(p.trending.entries[0],github_id=None,status='unresolved_identity')
    result = validate(replace(p,trending=replace(p.trending,entries=(entry,)),boundary_coverage='PARTIAL'))
    assert not result.trending_eligible
    with pytest.raises(ObservationValidationError,match='TRENDING_IDENTITY_MISMATCH'):
        validate(replace(p,trending=replace(p.trending,entries=(replace(p.trending.entries[0],github_id=99),))))


def test_forged_coverage_and_duration_are_rejected():
    with pytest.raises(ObservationValidationError,match='INCONSISTENT_COLLECTION_OR_COVERAGE'):
        validate(replace(payload(4000),boundary_coverage='FULL'))
    p = payload()
    repo = replace(p.repositories[0],diagnostics=SafeDiagnostics(http_status=200,duration_ms=99999))
    with pytest.raises(ObservationValidationError,match='CLOCK_DURATION_MISMATCH'):
        validate(replace(p,repositories=(repo,)))


@pytest.mark.parametrize('mutation',[
    lambda p:replace(p,fixture=False),
    lambda p:replace(p,mode='production-observation'),
    lambda p:replace(p,pool_ref=replace(p.pool_ref,key=p.pool_ref.key.replace('test-observation-pools/v1/',
        'observation-pools/v1/production/'))),
])
def test_fixture_provenance_cannot_be_relabelled_in_isolation(mutation):
    with pytest.raises(ObservationValidationError):
        validate(mutation(payload()))


def test_unavailable_values_future_pool_and_metadata_are_rejected():
    p = payload()
    bad = replace(p.repositories[0],status='unavailable',diagnostics=SafeDiagnostics(category='HTTP_ERROR',
                                                                                 http_status=404,duration_ms=1000))
    with pytest.raises(ObservationValidationError,match='UNAVAILABLE_HAS_VALUES'):
        validate(replace(p,repositories=(bad,)))
    with pytest.raises(ObservationValidationError,match='FUTURE_POOL_REFERENCE'):
        validate(replace(p,pool_ref=replace(p.pool_ref,key=p.pool_ref.key.replace('2026-11-01','2040-11-01'))))
    bad = replace(p.repositories[0],metadata=replace(p.repositories[0].metadata,created_at=NOW+timedelta(days=1)))
    with pytest.raises(ObservationValidationError,match='FUTURE_METADATA_TIMESTAMP'):
        validate(replace(p,repositories=(bad,)))


def test_pre_boundary_observation_preserved_as_none_coverage():
    p = payload()
    request,response = B-timedelta(seconds=2),B-timedelta(seconds=1)
    repo = replace(p.repositories[0],request_started_at=request,response_received_at=response,observed_at=response)
    page = replace(p.trending,request_started_at=request,response_received_at=response,observed_at=response)
    result = validate(replace(p,started_at=request,repositories=(repo,),trending=page,boundary_coverage='NONE'))
    assert not result.trending_eligible and result.eligible_repository_ids == ()
    assert result.envelope.payload.repositories[0].observed_at == response


def test_nested_depth_and_malformed_urls_fail_without_sensitive_echo():
    data = raw()
    nested = {}
    for _ in range(20):
        nested = {'secret-key':nested}
    data['payload']['extra'] = nested
    with pytest.raises(ObservationValidationError,match='JSON_COMPLEXITY_LIMIT'):
        validate_observation(data,now=NOW,namespace='fixture')
    p = payload()
    bad = replace(p.repositories[0],metadata=replace(p.repositories[0].metadata,
                                                   full_name='../repo',html_url='https://github.com/../repo'))
    with pytest.raises(ObservationValidationError):
        validate(replace(p,repositories=(bad,)))


def test_no_network_db_or_existing_production_service_is_touched(monkeypatch):
    import socket
    import httpx
    from database.db import Database
    from services.r2_persistence import R2DatabaseStore
    from collectors.github_client import GitHubClient
    from services.narrative_service import OpenAIJSONClient
    from telegram.sender import TelegramSender
    from tests.test_observation_pool import record, build
    def forbidden(*args,**kwargs):
        pytest.fail('Observation Phase 1/2 attempted external or production access')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(httpx.Client,'send',forbidden)
    for cls in (Database,R2DatabaseStore,GitHubClient,OpenAIJSONClient,TelegramSender):
        monkeypatch.setattr(cls,'__init__',forbidden)
    assert build([record()]).entries
    assert validate(payload()).eligible_repository_ids
