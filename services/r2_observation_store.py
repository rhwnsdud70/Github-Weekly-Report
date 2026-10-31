"""Bounded immutable JSON storage, separate from SQLite persistence and reporting.

No I/O on import/construction. All keys are generated from validated fields.
An instance is a single, non-concurrent operation context with a fixed UTC cutoff.
"""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta, timezone
import re

from config import observation_rules as rules
from models.observation_storage import ObjectReference, StoredObject, PublicationReceipt
from services.observation_pool_validation import validate_pool, encode_pool
from services.observation_validation import (
    ObservationValidationError, validate_observation, encode_observation, _time, _uuid,
)
from services.r2_persistence import create_r2_client, _safe_error, _missing_key
from services.scheduler import KST, report_period


PREFIXES = {
    ('observation','production'):'observations/v1/production/',
    ('pool','production'):'observation-pools/v1/production/',
    ('observation','fixture'):'test-observations/v1/',
    ('pool','fixture'):'test-observation-pools/v1/',
}


class R2ObservationError(RuntimeError):
    def __init__(self, operation, category, http_status=None):
        self.operation,self.category,self.http_status = operation,category,http_status
        super().__init__(f'Observation store {operation}: {category}; HTTP={http_status or "N/A"}')


class R2ObservationStore:
    def __init__(self, settings, *, as_of, namespace='production', profile='publication', client=None,
                 request_guard=None):
        if (not isinstance(namespace,str) or namespace not in {'production','fixture'} or
                not isinstance(profile,str) or profile not in rules.R2_STORE_BUDGETS):
            raise R2ObservationError('configure','INVALID_STORE_CONTEXT')
        try:
            settings.validate_r2()
        except Exception:
            raise R2ObservationError('configure','INVALID_R2_CONFIGURATION') from None
        self._settings,self._client = settings,client
        self._owns_client = client is None
        self.as_of = _time(as_of)
        self.boundary = report_period(self.as_of).end.astimezone(timezone.utc)
        self.namespace,self.profile = namespace,profile
        self.logical_requests = self.physical_attempts = 0
        self._limits = rules.R2_STORE_BUDGETS[profile]
        self._registered = self._active_call = self._closed = False
        self._handler_id = 'observation-store-'+str(id(self))
        self._get_counts = {}
        self._request_guard = request_guard

    def advance_cutoff(self, now):
        """Advance within this boundary only; retain all request counters and scopes."""
        now = _time(now)
        if now < self.as_of or report_period(now).end != self.boundary:
            raise R2ObservationError('configure','INVALID_CUTOFF_ADVANCE')
        self.as_of = now

    def set_request_guard(self, guard):
        """Install a job deadline before any requests; never reset budget counters."""
        if self.logical_requests or self._request_guard is not None or not callable(guard):
            raise R2ObservationError('configure','INVALID_REQUEST_GUARD')
        self._request_guard = guard

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()

    def close(self):
        if self._closed:
            return
        try:
            if self._registered:
                self._client.meta.events.unregister('before-send.s3',unique_id=self._handler_id)
                self._registered = False
            if self._owns_client and self._client is not None:
                self._client.close()
        except Exception:
            raise R2ObservationError('close','CLIENT_CLOSE_ERROR') from None
        finally:
            self._closed = True

    def _before_send(self, request=None, **kwargs):
        if self._active_call:
            if self._request_guard is not None:
                self._request_guard()
            if self.physical_attempts >= self._limits[1]:
                raise R2ObservationError('request','REQUEST_BUDGET_EXHAUSTED')
            self.physical_attempts += 1

    def _get_client(self):
        if self._closed:
            raise R2ObservationError('configure','STORE_CLOSED')
        if self._client is None:
            try:
                self._client = create_r2_client(self._settings)
            except Exception as error:
                # Factory diagnostics are already allowlisted; do not echo SDK text.
                from services.r2_persistence import R2PersistenceError
                if isinstance(error,R2PersistenceError):
                    raise R2ObservationError('configure',error.category,error.http_status) from None
                raise R2ObservationError('configure','SDK_CONFIGURATION_ERROR') from None
        if not self._registered:
            try:
                retries = self._client.meta.config.retries
                attempts = retries.get('total_max_attempts')
                if attempts is None and type(retries.get('max_attempts')) is int:
                    attempts = retries['max_attempts']+1
                if type(attempts) is not int or not 1 <= attempts <= 3:
                    raise ValueError()
                self._client.meta.events.register('before-send.s3',self._before_send,unique_id=self._handler_id)
                self._registered = True
            except Exception:
                raise R2ObservationError('configure','UNSUPPORTED_RETRY_OR_TRACKING_POLICY') from None
        return self._client

    def _call(self, operation, **kwargs):
        if self._request_guard is not None:
            self._request_guard()
        target = kwargs.get('Prefix' if operation == 'list_objects_v2' else 'Key')
        if operation not in {'put_object','get_object','list_objects_v2'} or not self._valid_target(operation,target):
            raise R2ObservationError('request','FORBIDDEN_OBJECT_PREFIX')
        if self._active_call:
            raise R2ObservationError('request','CONCURRENT_USE_UNSUPPORTED')
        if self.logical_requests >= self._limits[0] or self.physical_attempts >= self._limits[1]:
            raise R2ObservationError(operation,'REQUEST_BUDGET_EXHAUSTED')
        client = self._get_client()
        self.logical_requests += 1
        before = self.physical_attempts
        self._active_call = True
        try:
            response = getattr(client,operation)(Bucket=self._settings.r2_bucket,**kwargs)
            if before == self.physical_attempts:
                body = response.get('Body') if isinstance(response,dict) else None
                if body is not None:
                    body.close()
                raise R2ObservationError(operation,'ATTEMPT_TRACKING_UNAVAILABLE')
            return response
        finally:
            self._active_call = False

    def _valid_target(self, operation, target):
        """Defense in depth: even internal transport rejects traversal/raw key shapes."""
        if not isinstance(target,str):
            return False
        for kind in ('observation','pool'):
            prefix = PREFIXES[(kind,self.namespace)]
            weeks = 1 if operation == 'put_object' else 2 if kind == 'observation' else rules.COMPLETED_WEEKS
            dates = {(self.boundary-timedelta(weeks=i)).astimezone(KST).date().isoformat() for i in range(weeks)}
            if operation == 'list_objects_v2':
                if target in {prefix+date+'/' for date in dates}:
                    return True
            else:
                match = re.fullmatch(re.escape(prefix)+r'(\d{4}-\d{2}-\d{2})/([0-9a-f-]{36})\.json',target)
                if match and match[1] in dates:
                    try:
                        _uuid(match[2])
                        return True
                    except ObservationValidationError:
                        return False
        return False

    def _reference(self, kind, boundary, object_id, *, publishing=False):
        boundary = _time(boundary)
        _uuid(object_id)
        if report_period(boundary).end != boundary:
            raise R2ObservationError('key','INVALID_BOUNDARY')
        allowed = ({self.boundary} if publishing else
                   {self.boundary-timedelta(weeks=i) for i in range(2 if kind == 'observation' else rules.COMPLETED_WEEKS)})
        if boundary not in allowed:
            raise R2ObservationError('key','BOUNDARY_OUTSIDE_SCOPE')
        prefix = PREFIXES[(kind,self.namespace)]
        date = boundary.astimezone(KST).date().isoformat()
        return ObjectReference(kind,self.namespace,boundary,object_id,prefix+date+'/'+object_id+'.json')

    def _validate(self, kind, value):
        validator = validate_observation if kind == 'observation' else validate_pool
        return validator(value,now=self.as_of,namespace=self.namespace)

    def _encode(self, kind, envelope):
        return (encode_observation if kind == 'observation' else encode_pool)(envelope)

    def _get(self, ref, *, verification=False):
        group = (ref.kind,ref.boundary)
        limit = rules.OBJECTS_PER_BOUNDARY if ref.kind == 'observation' else rules.POOL_GET_LIMIT
        count_key = group if ref.kind == 'observation' else ('pool',)
        if self._get_counts.get(count_key,0) >= limit:
            raise R2ObservationError('get','OBJECT_GET_LIMIT')
        self._get_counts[count_key] = self._get_counts.get(count_key,0)+1
        try:
            response = self._call('get_object',Key=ref.object_key)
        except R2ObservationError:
            raise
        except Exception as error:
            if _missing_key(error):
                return None
            safe = _safe_error('get',error)
            raise R2ObservationError('get',safe.category,safe.http_status) from None
        if not isinstance(response,dict) or not callable(getattr(response.get('Body'),'read',None)):
            body = response.get('Body') if isinstance(response,dict) else None
            if callable(getattr(body,'close',None)):
                body.close()
            raise R2ObservationError('get','INVALID_GET_RESPONSE')
        try:
            # Always close Body, including invalid header/size/JSON responses.
            with closing(response['Body']) as body:
                size = response.get('ContentLength')
                if type(size) is not int or not 0 <= size <= rules.MAX_JSON_BYTES:
                    raise R2ObservationError('get','INVALID_CONTENT_LENGTH')
                modified = _time(response['LastModified'])
                if not verification and modified > self.as_of:
                    raise R2ObservationError('get','OBJECT_AFTER_CUTOFF')
                chunks,received = [],0
                while received <= rules.MAX_JSON_BYTES:
                    chunk = body.read(min(65536,rules.MAX_JSON_BYTES+1-received))
                    if not isinstance(chunk,bytes):
                        raise R2ObservationError('get','INVALID_RESPONSE_BODY')
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                raw = b''.join(chunks)
                if len(raw) != size:
                    raise R2ObservationError('get','CONTENT_LENGTH_MISMATCH')
        except R2ObservationError:
            raise
        except ObservationValidationError as error:
            raise R2ObservationError('get',error.category) from None
        except Exception as error:
            safe = _safe_error('get',error)
            raise R2ObservationError('get',safe.category,safe.http_status) from None
        try:
            valid = self._validate(ref.kind,raw)
        except ObservationValidationError as error:
            raise R2ObservationError('get',error.category) from None
        payload = valid.envelope.payload
        object_id = payload.observation_id if ref.kind == 'observation' else payload.pool_id
        if payload.boundary_end != ref.boundary or object_id != ref.object_id:
            raise R2ObservationError('get','KEY_PAYLOAD_MISMATCH')
        if payload.publication_status != 'UNCONFIRMED':
            raise R2ObservationError('get','INVALID_IMMUTABLE_PUBLICATION_STATUS')
        # Only canonical bytes are owned by this store. No implicit remote rewrite.
        if raw != self._encode(ref.kind,valid.envelope):
            raise R2ObservationError('get','NON_CANONICAL_STORED_OBJECT')
        return StoredObject(replace(ref,last_modified=modified),valid,size)

    def _publish(self, kind, envelope):
        valid = self._validate(kind,envelope)
        envelope = valid.envelope
        payload = envelope.payload
        if payload.publication_status != 'UNCONFIRMED':
            raise R2ObservationError('publish','INVALID_IMMUTABLE_PUBLICATION_STATUS')
        object_id = payload.observation_id if kind == 'observation' else payload.pool_id
        ref = self._reference(kind,payload.boundary_end,object_id,publishing=True)
        encoded = self._encode(kind,envelope)
        uncertainty = None
        before_put = self.physical_attempts
        try:
            self._call('put_object',Key=ref.object_key,Body=encoded,ContentType='application/json',IfNoneMatch='*')
        except R2ObservationError as error:
            if self.physical_attempts == before_put:
                raise
            uncertainty = error
        except Exception as error:
            safe = _safe_error('publish',error)
            # A timeout or precondition failure may follow a committed PUT whose
            # acknowledgement was lost. Confirm ONLY this ID and these bytes.
            if safe.http_status != 412 and safe.category not in {'NETWORK_ERROR','HTTP_ERROR'}:
                raise R2ObservationError('publish',safe.category,safe.http_status) from None
            if safe.category == 'HTTP_ERROR' and (safe.http_status is None or safe.http_status < 500):
                raise R2ObservationError('publish',safe.category,safe.http_status) from None
            uncertainty = R2ObservationError('publish',safe.category,safe.http_status)
        try:
            stored = self._get(ref,verification=True)
        except R2ObservationError as error:
            if error.category in {'NETWORK_ERROR','REQUEST_BUDGET_EXHAUSTED','OBJECT_GET_LIMIT',
                                  'RATE_LIMIT','AUTHENTICATION_OR_PERMISSION','HTTP_ERROR','SDK_ERROR'}:
                return self._receipt(ref,envelope,None,'UNCONFIRMED',error)
            if uncertainty is not None and uncertainty.http_status == 412:
                raise R2ObservationError('verify','IMMUTABLE_OBJECT_CONFLICT',412) from None
            raise
        if stored is None:
            error = uncertainty or R2ObservationError('verify','OBJECT_NOT_FOUND')
            return self._receipt(ref,envelope,None,'UNCONFIRMED',error)
        if self._encode(kind,stored.validation.envelope) != encoded:
            raise R2ObservationError('verify','IMMUTABLE_OBJECT_CONFLICT',412 if uncertainty else None)
        return self._receipt(ref,envelope,stored.byte_size,'VERIFIED')

    @staticmethod
    def _receipt(ref, envelope, size, status, error=None):
        return PublicationReceipt(ref.object_key,ref.object_id,envelope.integrity.payload_sha256,
            envelope.schema_version,size,ref.boundary,status,
            'SCHEMA_SHA256_BYTES_MATCH' if status == 'VERIFIED' else 'NOT_CONFIRMED',
            error.category if error else None,error.http_status if error else None)

    def publish_observation(self, envelope):
        return self._publish('observation',envelope)

    def publish_pool(self, envelope):
        return self._publish('pool',envelope)

    def get_observation(self, boundary, observation_id):
        return self._get(self._reference('observation',boundary,observation_id))

    def get_pool(self, boundary, pool_id):
        return self._get(self._reference('pool',boundary,pool_id))

    def _list(self, kind, boundary):
        ref = self._reference(kind,boundary,'00000000-0000-4000-8000-000000000000')
        prefix = ref.object_key.rsplit('/',1)[0]+'/'
        try:
            response = self._call('list_objects_v2',Prefix=prefix,MaxKeys=rules.OBJECTS_PER_BOUNDARY+1)
        except R2ObservationError:
            raise
        except Exception as error:
            safe = _safe_error('list',error)
            raise R2ObservationError('list',safe.category,safe.http_status) from None
        if not isinstance(response,dict) or type(response.get('IsTruncated')) is not bool:
            raise R2ObservationError('list','INVALID_LIST_RESPONSE')
        if response['IsTruncated']:
            raise R2ObservationError('list','INCOMPLETE_LIST')
        rows = response.get('Contents',[])
        if not isinstance(rows,list):
            raise R2ObservationError('list','INVALID_LIST_RESPONSE')
        if (('KeyCount' in response and (type(response['KeyCount']) is not int or response['KeyCount'] != len(rows))) or
                response.get('NextContinuationToken') or response.get('Prefix',prefix) != prefix or
                response.get('Name',self._settings.r2_bucket) != self._settings.r2_bucket):
            raise R2ObservationError('list','INVALID_LIST_RESPONSE')
        if len(rows)>rules.OBJECTS_PER_BOUNDARY:
            raise R2ObservationError('list','OBJECT_LIMIT')
        refs,seen = [],set()
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('Key'),str):
                raise R2ObservationError('list','INVALID_LIST_RESPONSE')
            match = re.fullmatch(re.escape(prefix)+r'([0-9a-f-]{36})\.json',row['Key'])
            if not match or row['Key'] in seen:
                raise R2ObservationError('list','LIST_PREFIX_OR_KEY_VIOLATION')
            seen.add(row['Key'])
            try:
                candidate = self._reference(kind,boundary,match[1])
            except ObservationValidationError:
                raise R2ObservationError('list','LIST_PREFIX_OR_KEY_VIOLATION') from None
            try:
                modified = _time(row['LastModified'])
            except (KeyError,ObservationValidationError):
                raise R2ObservationError('list','INVALID_LIST_TIMESTAMP') from None
            if modified <= self.as_of:
                refs.append(replace(candidate,last_modified=modified))
        return tuple(sorted(refs,key=lambda r:(r.last_modified,r.object_key),reverse=True))

    def list_observations(self, boundary):
        """One complete bounded page for current/previous report boundary only."""
        return self._list('observation',boundary)

    def list_recent_pools(self):
        """At most four complete pages; returns refs, not consumer selection."""
        return tuple(ref for i in range(rules.COMPLETED_WEEKS)
                     for ref in self._list('pool',self.boundary-timedelta(weeks=i)))
