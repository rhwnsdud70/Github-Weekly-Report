from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import io
import json
from types import SimpleNamespace

import pytest

from services.observation_pool import pool_manifest
from services.observation_pool_validation import (
    seal_pool, encode_pool, validate_pool, pool_payload_sha256,
)
from services.observation_validation import (
    ObservationValidationError, seal_observation, encode_observation,
)
from services.r2_observation_store import R2ObservationStore, R2ObservationError, PREFIXES
from tests.test_observation_validation import payload, B, NOW, OBS_ID
from tests.test_observation_pool import build, record, NOW as POOL_NOW
from tests.test_r2_persistence import FakeS3, FakeClientError, settings, PRODUCTION_KEY


class Events:
    def __init__(self): self.handlers = {}
    def register(self,event,handler,unique_id): self.handlers[unique_id] = handler
    def unregister(self,event,unique_id): self.handlers.pop(unique_id,None)
    def fire(self):
        for handler in tuple(self.handlers.values()): handler(request=None)


class ObservationS3(FakeS3):
    """SDK-contract fake with atomic conditional PUT and before-send events."""
    def __init__(self, **kwargs):
        super().__init__(production=b'protected production bytes',**kwargs)
        self.objects['backups/protected.db'] = b'protected backup bytes'
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts':1}),events=Events())
        self.keys, self.modified = [],{}
        self.put_mode = None
        self.corrupt = None
        self.list_rows = None
        self.truncated = False
        self.get_header_delta = 0
        self.close_count = 0

    def send(self,operation,key):
        self.meta.events.fire()
        self.record(operation)
        self.keys.append(key)
        assert not key.startswith(('production/','backups/'))

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch):
        self.send('put',Key)
        assert IfNoneMatch == '*' and ContentType == 'application/json'
        if Key in self.objects:
            raise FakeClientError('PreconditionFailed',412)
        if self.put_mode == 'drop':
            raise OSError('sensitive endpoint/access key')
        self.objects[Key] = Body
        self.modified[Key] = NOW
        if self.put_mode == 'commit-timeout':
            raise OSError('sensitive endpoint/access key')
        return {}

    def get_object(self, Bucket, Key):
        self.send('get',Key)
        if Key not in self.objects:
            raise FakeClientError('NoSuchKey',404)
        data = self.objects[Key]
        if self.corrupt == 'json': data = b'not json sensitive-token'
        if self.corrupt == 'checksum':
            value = json.loads(data)
            value['integrity']['payload_sha256'] = '0'*64
            data = json.dumps(value).encode()
        self.body = io.BytesIO(data)
        return {'Body':self.body,'ContentLength':len(data)+self.get_header_delta,
                'LastModified':self.modified.get(Key,NOW)}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        self.send('list',Prefix)
        rows = self.list_rows
        if rows is None:
            rows = [{'Key':key,'LastModified':self.modified.get(key,NOW)}
                    for key in sorted(self.objects) if key.startswith(Prefix)]
        return {'IsTruncated':self.truncated or len(rows)>MaxKeys,'Contents':rows[:MaxKeys]}

    def copy_object(self, **kwargs): pytest.fail('Observation store attempted remote copy')
    def delete_object(self, **kwargs): pytest.fail('Observation store attempted remote delete')
    def close(self): self.close_count += 1


def store(tmp_path, client, *, namespace='fixture', profile='publication', **kwargs):
    # A path is present in Settings but the store must never open it.
    return R2ObservationStore(settings(tmp_path/'must-not-exist.db'),as_of=NOW,
                              namespace=namespace,profile=profile,client=client,**kwargs)


def pool_envelope():
    return seal_pool(build([record()]),pool_id=OBS_ID,created_at=POOL_NOW+timedelta(minutes=1),
                     producer=payload().producer)


@pytest.mark.parametrize('kind',['observation','pool'])
def test_publish_verifies_real_get_and_returns_frozen_receipt(tmp_path,kind):
    client = ObservationS3()
    envelope = seal_observation(payload()) if kind == 'observation' else pool_envelope()
    with store(tmp_path,client) as storage:
        receipt = getattr(storage,'publish_'+kind)(envelope)
        assert receipt.publication_status == 'VERIFIED'
        assert receipt.sha256 == envelope.integrity.payload_sha256
        assert receipt.byte_size == len(client.objects[receipt.object_key])
        assert storage.logical_requests == storage.physical_attempts == 2
        assert client.calls == ['put','get'] and client.body.closed
        with pytest.raises(FrozenInstanceError): receipt.publication_status = 'FAILED'
    assert not client.meta.events.handlers and client.close_count == 0
    assert envelope.payload.publication_status == 'UNCONFIRMED'
    assert client.objects[PRODUCTION_KEY] == b'protected production bytes'
    assert client.objects['backups/protected.db'] == b'protected backup bytes'
    assert not (tmp_path/'must-not-exist.db').exists()


@pytest.mark.parametrize('kind',['observation','pool'])
def test_identical_retry_412_is_idempotent(tmp_path,kind):
    client = ObservationS3()
    envelope = seal_observation(payload()) if kind == 'observation' else pool_envelope()
    for _ in range(2):
        with store(tmp_path,client) as storage:
            receipt = getattr(storage,'publish_'+kind)(envelope)
            assert receipt.publication_status == 'VERIFIED'
    assert client.calls == ['put','get','put','get']


@pytest.mark.parametrize('kind',['observation','pool'])
def test_same_key_different_valid_content_is_conflict(tmp_path,kind):
    client = ObservationS3()
    envelope = seal_observation(payload()) if kind == 'observation' else pool_envelope()
    with store(tmp_path,client) as storage:
        receipt = getattr(storage,'publish_'+kind)(envelope)
    original = client.objects[receipt.object_key]
    p = payload()
    changed = replace(p,repositories=(replace(p.repositories[0],stars=1500),))
    changed_envelope = (seal_observation(changed) if kind == 'observation' else
        seal_pool(build([record(2)]),pool_id=OBS_ID,created_at=POOL_NOW+timedelta(minutes=1),producer=p.producer))
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='IMMUTABLE_OBJECT_CONFLICT'):
        getattr(storage,'publish_'+kind)(changed_envelope)
    assert client.objects[receipt.object_key] == original


@pytest.mark.parametrize('mode,expected',[('commit-timeout','VERIFIED'),('drop','UNCONFIRMED')])
def test_put_timeout_checks_only_same_id_and_bytes(tmp_path,mode,expected):
    client = ObservationS3()
    client.put_mode = mode
    with store(tmp_path,client) as storage:
        receipt = storage.publish_observation(seal_observation(payload()))
        assert receipt.publication_status == expected
    assert client.calls == ['put','get'] and len(set(client.keys)) == 1
    assert receipt.byte_size is None if expected == 'UNCONFIRMED' else receipt.byte_size > 0


def test_get_timeout_never_returns_verified_receipt(tmp_path):
    client = ObservationS3(fail={'get':OSError('sensitive-secret')})
    with store(tmp_path,client) as storage:
        receipt = storage.publish_observation(seal_observation(payload()))
    assert receipt.publication_status == 'UNCONFIRMED'
    assert receipt.error_category == 'NETWORK_ERROR'
    assert 'sensitive-secret' not in repr(receipt)


@pytest.mark.parametrize('error,category',[
    (FakeClientError('AccessDenied',403),'AUTHENTICATION_OR_PERMISSION'),
    (FakeClientError('InvalidAccessKeyId',401),'AUTHENTICATION_OR_PERMISSION'),
    (FakeClientError('SlowDown',429),'RATE_LIMIT'),
    (FakeClientError('NoSuchBucket',404),'BUCKET_NOT_FOUND'),
    (FakeClientError('404',404),'HTTP_ERROR'),
    (OSError('sensitive-secret'),'NETWORK_ERROR'),
])
def test_get_errors_are_distinct_from_not_found_and_safe(tmp_path,error,category):
    client = ObservationS3(fail={'get':error})
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError) as caught:
        storage.get_observation(B,OBS_ID)
    assert caught.value.category == category and 'sensitive' not in str(caught.value)


def test_missing_object_returns_none_but_missing_bucket_is_error(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        assert storage.get_observation(B,OBS_ID) is None
    assert client.calls == ['get']


@pytest.mark.parametrize('corruption,category',[('json','INVALID_JSON'),('checksum','CHECKSUM_MISMATCH')])
def test_post_put_corruption_fails_verification(tmp_path,corruption,category):
    client = ObservationS3()
    client.corrupt = corruption
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError) as caught:
        storage.publish_observation(seal_observation(payload()))
    assert caught.value.category == category and client.body.closed
    assert 'sensitive-token' not in str(caught.value)


def test_bad_length_closes_body_and_never_succeeds(tmp_path):
    client = ObservationS3()
    client.get_header_delta = 1
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='CONTENT_LENGTH_MISMATCH'):
        storage.publish_observation(seal_observation(payload()))
    assert client.body.closed


def test_wrong_namespace_or_forged_publication_status_rejected_before_io(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client,namespace='production') as storage, pytest.raises(ObservationValidationError):
        storage.publish_observation(seal_observation(payload()))
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='INVALID_IMMUTABLE_PUBLICATION_STATUS'):
        storage.publish_observation(seal_observation(replace(payload(),publication_status='VERIFIED')))
    assert client.calls == []


def test_normal_reads_and_boundary_listing(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        storage.publish_observation(seal_observation(payload()))
    with store(tmp_path,client,profile='report_read') as storage:
        references = storage.list_observations(B)
        assert len(references) == 1 and references[0].object_id == OBS_ID
        loaded = storage.get_observation(B,OBS_ID)
        assert loaded.validation.eligible_repository_ids == (900001,)
        assert loaded.reference == references[0]
        assert storage.list_observations(B-timedelta(days=7)) == ()


def test_pool_get_and_recent_four_lists(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        storage.publish_pool(pool_envelope())
    with store(tmp_path,client,profile='observation_job') as storage:
        refs = storage.list_recent_pools()
        assert len(refs) == 1
        loaded = storage.get_pool(B,OBS_ID)
        assert len(loaded.validation.pool.entries) == 1
    assert client.calls.count('list') == 4


@pytest.mark.parametrize('delta,kind',[(7,'observation'),(-14,'observation'),(7,'pool'),(-28,'pool')])
def test_future_and_out_of_scope_boundaries_blocked_without_io(tmp_path,delta,kind):
    client = ObservationS3()
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='BOUNDARY_OUTSIDE_SCOPE'):
        getattr(storage,'get_'+kind)(B+timedelta(days=delta),OBS_ID)
    assert client.calls == []


@pytest.mark.parametrize('object_id',['../production/github_weekly_report.db','secret-token','0'*36])
def test_malformed_id_cannot_be_used_as_key(tmp_path,object_id):
    client = ObservationS3()
    with store(tmp_path,client) as storage, pytest.raises(ObservationValidationError):
        storage.get_observation(B,object_id)
    assert not client.calls


@pytest.mark.parametrize('condition,category',[('truncated','INCOMPLETE_LIST'),('many','OBJECT_LIMIT'),
                                            ('prefix','LIST_PREFIX_OR_KEY_VIOLATION')])
def test_incomplete_large_or_polluted_list_is_not_partially_accepted(tmp_path,condition,category):
    client = ObservationS3()
    prefix = PREFIXES[('observation','fixture')]+'2026-11-01/'
    client.list_rows = [{'Key':prefix+f'00000000-0000-4000-8000-{i:012d}.json','LastModified':NOW} for i in range(9 if condition=='many' else 1)]
    if condition == 'truncated': client.truncated = True
    if condition == 'prefix': client.list_rows[0]['Key'] = PRODUCTION_KEY
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match=category):
        storage.list_observations(B)
    assert client.calls == ['list']


def test_list_failure_is_not_empty_list(tmp_path):
    client = ObservationS3(fail={'list':OSError('sensitive')})
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='NETWORK_ERROR'):
        storage.list_observations(B)


def test_key_and_payload_id_or_boundary_mismatch_rejected(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        receipt = storage.publish_observation(seal_observation(payload()))
    other = '00000000-0000-4000-8000-000000000002'
    client.objects[receipt.object_key.replace(OBS_ID,other)] = client.objects[receipt.object_key]
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='KEY_PAYLOAD_MISMATCH'):
        storage.get_observation(B,other)


def test_logical_budget_stops_before_another_sdk_call(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        assert storage.get_observation(B,OBS_ID) is None
        assert storage.get_observation(B,OBS_ID) is None
        with pytest.raises(R2ObservationError,match='REQUEST_BUDGET_EXHAUSTED'):
            storage.get_observation(B,OBS_ID)
    assert client.calls == ['get','get']


def test_sdk_retries_are_counted_and_physical_limit_enforced(tmp_path):
    client = ObservationS3()
    client.meta.config.retries = {'total_max_attempts':3}
    # Deliberately hostile fake retry loop: before-send still stops wire attempts.
    def many_gets(**kwargs):
        for _ in range(100): client.send('get',kwargs['Key'])
    client.get_object = many_gets
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='REQUEST_BUDGET_EXHAUSTED'):
        storage.get_observation(B,OBS_ID)
    assert storage.logical_requests == 1 and storage.physical_attempts == 6
    assert len(client.calls) == 6


def test_unknown_sdk_retry_policy_rejected_before_network(tmp_path):
    client = ObservationS3()
    client.meta.config.retries = {'total_max_attempts':99}
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='UNSUPPORTED_RETRY_OR_TRACKING_POLICY'):
        storage.get_observation(B,OBS_ID)
    assert not client.calls


def test_get_and_list_respect_fixed_cutoff(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        receipt = storage.publish_observation(seal_observation(payload()))
    client.modified[receipt.object_key] = NOW+timedelta(seconds=1)
    with store(tmp_path,client) as storage:
        assert storage.list_observations(B) == ()
        with pytest.raises(R2ObservationError,match='OBJECT_AFTER_CUTOFF'):
            storage.get_observation(B,OBS_ID)


def test_pool_envelope_roundtrip_and_recomputed_builder_contract():
    envelope = pool_envelope()
    valid = validate_pool(encode_pool(envelope),now=NOW,namespace='fixture')
    assert valid.pool == build([record()])
    assert valid.envelope.payload.manifest == pool_manifest(build([record()]),as_of=POOL_NOW,fixture=True)
    data = json.loads(encode_pool(envelope))
    data['payload']['manifest']['entries'][0]['priority'] = 0
    data['integrity']['payload_sha256'] = pool_payload_sha256(data['payload'])
    with pytest.raises(ObservationValidationError,match='POOL_BUILDER_MISMATCH'):
        validate_pool(data,now=NOW,namespace='fixture')


@pytest.mark.parametrize('mutation',[
    lambda m:m['entries'].append(m['entries'][0]),
    lambda m:m['entries'][0]['record'].update(github_id=True),
    lambda m:m['entries'][0]['record'].update(last_seen_at='2040-11-01T12:00:00Z'),
    lambda m:m.update(fixture=False),
    lambda m:m['entries'][0]['record'].update(source_kind='sensitive-token'),
    lambda m:m.update(entries=m['entries']*41),
])
def test_invalid_pool_manifest_rejected_before_put(tmp_path,mutation):
    data = json.loads(encode_pool(pool_envelope()))
    mutation(data['payload']['manifest'])
    data['integrity']['payload_sha256'] = pool_payload_sha256(data['payload'])
    client = ObservationS3()
    with store(tmp_path,client) as storage, pytest.raises(ObservationValidationError) as caught:
        storage.publish_pool(data)
    assert not client.calls and 'sensitive-token' not in str(caught.value)


def test_real_botocore_conditional_put_retry_contract_without_network(tmp_path,monkeypatch):
    """Exercise installed SDK signing/serialization/events with an in-memory HTTP transport."""
    import socket
    from email.utils import format_datetime
    import botocore.endpoint
    import botocore.httpsession
    from botocore.awsrequest import AWSResponse
    from botocore.exceptions import ReadTimeoutError
    from services.r2_persistence import create_r2_client
    def forbidden(*args,**kwargs): pytest.fail('SDK test attempted a real socket connection')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setenv('AWS_EC2_METADATA_DISABLED','true')
    # Keep the fake transport focused on conditional PUT rather than aws-chunked framing.
    monkeypatch.setenv('AWS_REQUEST_CHECKSUM_CALCULATION','when_required')
    monkeypatch.setattr(botocore.endpoint.time,'sleep',lambda _:None)
    class Raw(io.BytesIO):
        def stream(self,amt=None,decode_content=False):
            yield self.read()
    wire,objects = [],{}
    def transport(self,request):
        wire.append(request.method)
        if request.method == 'PUT':
            assert request.headers.get('If-None-Match') in ('*',b'*')
            if not objects:
                body = request.body if isinstance(request.body,bytes) else request.body.read()
                objects['bytes'] = body
                raise ReadTimeoutError(endpoint_url='https://offline.invalid')
            return AWSResponse(request.url,412,{'Content-Type':'application/xml'},
                               Raw(b'<Error><Code>PreconditionFailed</Code></Error>'))
        assert request.method == 'GET'
        body = objects['bytes']
        return AWSResponse(request.url,200,{'Content-Length':str(len(body)),
            'Last-Modified':format_datetime(NOW,usegmt=True)},Raw(body))
    monkeypatch.setattr(botocore.httpsession.URLLib3Session,'send',transport)
    configured = settings(tmp_path/'never-open.db')
    client = create_r2_client(configured)
    try:
        assert 'IfNoneMatch' in client.meta.service_model.operation_model('PutObject').input_shape.members
        with store(tmp_path,client) as storage:
            receipt = storage.publish_observation(seal_observation(payload()))
            assert receipt.publication_status == 'VERIFIED'
            assert storage.logical_requests == 2 and storage.physical_attempts == 3
        assert wire == ['PUT','PUT','GET']
    finally:
        client.close()


@pytest.mark.parametrize('kind',['observation','pool'])
def test_production_namespace_uses_only_approved_keys(tmp_path,kind):
    client = ObservationS3()
    if kind == 'observation':
        envelope = seal_observation(payload(fixture=False))
    else:
        from tests.test_observation_pool import NOW as as_of
        from services.observation_pool import build_pool
        result = build_pool([replace(record(),fixture=False)],as_of=as_of)
        envelope = seal_pool(result,pool_id=OBS_ID,created_at=POOL_NOW+timedelta(minutes=1),producer=payload().producer)
    with store(tmp_path,client,namespace='production') as storage:
        receipt = getattr(storage,'publish_'+kind)(envelope)
    assert receipt.object_key.startswith(PREFIXES[(kind,'production')])
    assert not any(key.startswith(('production/','backups/')) for key in client.keys)


def test_pool_checksum_duplicate_json_key_and_size_are_validated():
    data = json.loads(encode_pool(pool_envelope()))
    data['integrity']['payload_sha256'] = '0'*64
    with pytest.raises(ObservationValidationError,match='CHECKSUM_MISMATCH'):
        validate_pool(data,now=NOW,namespace='fixture')
    for value in ('{"schema_version":1,"schema_version":1}', '{"value":NaN}'):
        with pytest.raises(ObservationValidationError):
            validate_pool(value,now=NOW,namespace='fixture')
    with pytest.raises(ObservationValidationError,match='JSON_SIZE_LIMIT'):
        validate_pool(b' '*(1024*1024+1),now=NOW,namespace='fixture')


def test_pool_get_count_cannot_be_bypassed_with_many_ids(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client,profile='observation_job') as storage:
        for _ in range(4): assert storage.get_pool(B,OBS_ID) is None
        with pytest.raises(R2ObservationError,match='OBJECT_GET_LIMIT'):
            storage.get_pool(B,OBS_ID)
    assert client.calls == ['get']*4


def test_reading_previous_three_pool_boundaries_is_allowed(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client,profile='observation_job') as storage:
        for weeks in range(4):
            assert storage.get_pool(B-timedelta(weeks=weeks),OBS_ID) is None


def test_private_transport_also_rejects_production_db_and_backup_prefix(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        for key in (PRODUCTION_KEY,'backups/db.db','../production/db.db',
                    'test-observations/v1/../../production/db.db',
                    'test-observations/v1/2026-11-01/not-a-uuid.json'):
            with pytest.raises(R2ObservationError,match='FORBIDDEN_OBJECT_PREFIX'):
                storage._call('put_object',Key=key,Body=b'blocked')
    assert not client.calls


@pytest.mark.parametrize('kind',['observation','pool'])
def test_filename_boundary_must_match_payload(tmp_path,kind):
    client = ObservationS3()
    envelope = seal_observation(payload()) if kind == 'observation' else pool_envelope()
    encode = encode_observation if kind == 'observation' else encode_pool
    key = PREFIXES[(kind,'fixture')]+'2026-10-25/'+OBS_ID+'.json'
    client.objects[key] = encode(envelope)
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='KEY_PAYLOAD_MISMATCH'):
        getattr(storage,'get_'+kind)(B-timedelta(days=7),OBS_ID)


def test_get_fixture_cannot_masquerade_in_production_prefix(tmp_path):
    client = ObservationS3()
    key = PREFIXES[('observation','production')]+'2026-11-01/'+OBS_ID+'.json'
    client.objects[key] = encode_observation(seal_observation(payload()))
    with store(tmp_path,client,namespace='production') as storage, pytest.raises(R2ObservationError,match='FIXTURE_NAMESPACE_MISMATCH'):
        storage.get_observation(B,OBS_ID)


def test_no_body_is_invalid_response_not_empty_object(tmp_path):
    client = ObservationS3()
    def invalid_get(**kwargs):
        client.send('get',kwargs['Key'])
        return {'ContentLength':0}
    client.get_object = invalid_get
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='INVALID_GET_RESPONSE'):
        storage.get_observation(B,OBS_ID)


@pytest.mark.parametrize('operation',['put','list'])
@pytest.mark.parametrize('error,category',[
    (FakeClientError('AccessDenied',403),'AUTHENTICATION_OR_PERMISSION'),
    (FakeClientError('SlowDown',429),'RATE_LIMIT'),
    (FakeClientError('NoSuchBucket',404),'BUCKET_NOT_FOUND'),
])
def test_publish_and_list_safe_failure_categories(tmp_path,operation,error,category):
    client = ObservationS3(fail={operation:error})
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError) as caught:
        if operation == 'put': storage.publish_observation(seal_observation(payload()))
        else: storage.list_observations(B)
    assert caught.value.category == category
    assert 'Sensitive response' not in str(caught.value)


def test_pool_schema_and_source_period_are_checked(tmp_path):
    client = ObservationS3()
    data = json.loads(encode_pool(pool_envelope()))
    data['schema_version'] = 2
    with store(tmp_path,client) as storage, pytest.raises(ObservationValidationError,match='UNSUPPORTED_SCHEMA'):
        storage.publish_pool(data)
    data['schema_version'] = 1
    data['payload']['manifest']['entries'][0]['record']['source_period_end'] = '2026-11-01T12:00:00Z'
    data['integrity']['payload_sha256'] = pool_payload_sha256(data['payload'])
    with store(tmp_path,client) as storage, pytest.raises(ObservationValidationError):
        storage.publish_pool(data)
    assert not client.calls


def test_fragmented_body_stream_is_consumed_and_closed(tmp_path):
    client = ObservationS3()
    with store(tmp_path,client) as storage:
        receipt = storage.publish_observation(seal_observation(payload()))
    original_get = client.get_object
    class Fragmented(io.BytesIO):
        def read(self,amount=-1): return super().read(min(amount,17))
    def fragmented(**kwargs):
        response = original_get(**kwargs)
        old = response['Body']
        data = old.read()
        old.close()
        response['Body'] = client.body = Fragmented(data)
        return response
    client.get_object = fragmented
    with store(tmp_path,client) as storage:
        assert storage.get_observation(B,OBS_ID).byte_size == receipt.byte_size
    assert client.body.closed


def test_owned_client_is_lazy_and_closed_once(tmp_path,monkeypatch):
    import services.r2_observation_store as module
    client = ObservationS3()
    created = []
    def factory(_):
        created.append(True)
        return client
    monkeypatch.setattr(module,'create_r2_client',factory)
    storage = module.R2ObservationStore(settings(tmp_path/'never.db'),as_of=NOW,namespace='fixture')
    assert not created
    with storage:
        assert storage.publish_observation(seal_observation(payload())).publication_status == 'VERIFIED'
    storage.close()
    assert created == [True] and client.close_count == 1 and not client.meta.events.handlers


def test_missing_attempt_events_fail_closed_and_close_returned_body(tmp_path):
    client = ObservationS3()
    body = io.BytesIO(b'unknown')
    client.get_object = lambda **_: {'Body':body,'ContentLength':7,'LastModified':NOW}
    with store(tmp_path,client) as storage, pytest.raises(R2ObservationError,match='ATTEMPT_TRACKING_UNAVAILABLE'):
        storage.get_observation(B,OBS_ID)
    assert body.closed and storage.physical_attempts == 0


def test_report_read_budget_covers_two_complete_eight_object_boundaries(tmp_path):
    client = ObservationS3()
    for weeks in (0,1):
        delta = timedelta(weeks=weeks)
        p = payload()
        repo = replace(p.repositories[0],request_started_at=p.repositories[0].request_started_at-delta,
                       response_received_at=p.repositories[0].response_received_at-delta,
                       observed_at=p.repositories[0].observed_at-delta)
        page = replace(p.trending,request_started_at=p.trending.request_started_at-delta,
                       response_received_at=p.trending.response_received_at-delta,
                       observed_at=p.trending.observed_at-delta)
        for index in range(8):
            identifier = f'00000000-0000-4000-8000-{index:012d}'
            p_week = replace(p,observation_id=identifier,period_start=p.period_start-delta,boundary_end=B-delta,
                started_at=p.started_at-delta,completed_at=p.completed_at-delta,repositories=(repo,),trending=page,
                pool_ref=replace(p.pool_ref,key=p.pool_ref.key.replace('2026-11-01','2026-10-25') if weeks else p.pool_ref.key))
            date = '2026-10-25' if weeks else '2026-11-01'
            client.objects[PREFIXES[('observation','fixture')]+date+'/'+identifier+'.json'] = encode_observation(seal_observation(p_week))
    with store(tmp_path,client,profile='report_read') as storage:
        for boundary in (B,B-timedelta(weeks=1)):
            refs = storage.list_observations(boundary)
            assert len(refs) == 8
            for ref in refs: assert storage.get_observation(ref.boundary,ref.object_id)
        assert storage.logical_requests == storage.physical_attempts == 18
        with pytest.raises(R2ObservationError,match='REQUEST_BUDGET_EXHAUSTED'):
            storage.list_observations(B)
