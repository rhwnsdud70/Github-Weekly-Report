"""Isolated Phase 7B probe. No Settings, database, or production store dependency."""
from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
from uuid import uuid4


ENV_NAMES = ('OBS_R2_ENDPOINT', 'OBS_R2_ACCESS_KEY_ID',
             'OBS_R2_SECRET_ACCESS_KEY', 'OBS_R2_BUCKET')
STAGES = ('Configuration', 'Authentication', 'Bucket Access', 'Object PUT',
          'Object GET', 'SHA-256 Integrity', 'Conditional PUT', 'Cleanup')
ENDPOINT = re.compile(r'https://[a-f0-9]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com/?\Z')
BUCKET = re.compile(r'gwr-observation-test-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z')


class ProbeError(Exception):
    """Only fixed categories and HTTP status; never carry provider messages."""
    def __init__(self, category, status=None):
        super().__init__(category)
        self.category, self.status = category, status


@dataclass(frozen=True)
class ProbeSettings:
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
                raise ProbeError('MISSING_ENV: ' + name)
            values.append(value)
        settings = cls(*values)
        settings.validate()
        return settings

    def validate(self):
        if not ENDPOINT.fullmatch(self.endpoint):
            raise ProbeError('INVALID_FORMAT: OBS_R2_ENDPOINT')
        if not BUCKET.fullmatch(self.bucket) or len(self.bucket) > 63 or '--' in self.bucket:
            raise ProbeError('ISOLATED_BUCKET_REQUIRED: OBS_R2_BUCKET')
        for name, value in zip(ENV_NAMES[1:3], (self.access_key, self.secret_key)):
            if not isinstance(value, str) or not value.strip():
                raise ProbeError('MISSING_ENV: ' + name)


def validate_sdk_contract():
    """Read bundled service schema only: no boto3 client/credential discovery."""
    import botocore.session
    model = botocore.session.get_session().get_service_model('s3')
    if 'IfNoneMatch' not in model.operation_model('PutObject').input_shape.members:
        raise ProbeError('CONDITIONAL_PUT_NOT_SUPPORTED')


def create_probe_client(settings):
    import boto3
    from botocore.config import Config
    for logger in ('boto3', 'botocore', 's3transfer', 'urllib3'):
        logging.getLogger(logger).setLevel(logging.WARNING)
    return boto3.client('s3', endpoint_url=settings.endpoint, region_name='auto',
        aws_access_key_id=settings.access_key, aws_secret_access_key=settings.secret_key,
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                      connect_timeout=10, read_timeout=15,
                      retries={'mode': 'standard', 'total_max_attempts': 1}))


def safe_error(exc):
    if isinstance(exc, ProbeError):
        return exc
    response = getattr(exc, 'response', {})
    if not isinstance(response, dict):
        response = {}
    status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
    status = status if type(status) is int and 100 <= status <= 599 else None
    code = response.get('Error', {}).get('Code')
    categories = {'InvalidAccessKeyId': 'INVALID_ACCESS_KEY',
                  'SignatureDoesNotMatch': 'SIGNATURE_MISMATCH',
                  'InvalidToken': 'INVALID_CREDENTIALS',
                  'AccessDenied': 'ACCESS_DENIED', 'NoSuchBucket': 'BUCKET_NOT_FOUND',
                  'NoSuchKey': 'OBJECT_NOT_FOUND', 'PreconditionFailed': 'PRECONDITION_FAILED',
                  'SlowDown': 'RATE_LIMIT', 'NotImplemented': 'CONDITIONAL_PUT_NOT_SUPPORTED'}
    category = categories.get(code)
    if category is None:
        name = type(exc).__name__
        if 'Timeout' in name or isinstance(exc, TimeoutError):
            category = 'TIMEOUT'
        elif name == 'ParamValidationError':
            category = 'SDK_PARAMETER_REJECTED'
        elif name in ('EndpointConnectionError', 'ConnectionClosedError', 'ProxyConnectionError') or isinstance(exc, OSError):
            category = 'NETWORK_ERROR'
        else:
            category = {401: 'INVALID_CREDENTIALS', 403: 'ACCESS_DENIED',
                        412: 'PRECONDITION_FAILED', 429: 'RATE_LIMIT'}.get(status, 'SDK_ERROR')
    return ProbeError(category, status)


def _http(response):
    status = response.get('ResponseMetadata', {}).get('HTTPStatusCode') if isinstance(response, dict) else None
    if type(status) is not int or not 200 <= status < 300:
        raise ProbeError('INVALID_HTTP_RESPONSE', status if type(status) is int else None)
    return status


@dataclass(frozen=True)
class ProbeResult:
    stages: tuple[tuple[str, str], ...]
    diagnostics: tuple[str, ...]
    final_status: str
    exit_code: int

    def lines(self):
        return ('Observation R2 Connectivity Check', *(f'{k}: {v}' for k, v in self.stages),
                *self.diagnostics, 'Production Database: NOT ACCESSED',
                'Production Claim: NOT MODIFIED', 'Telegram: NOT ACCESSED',
                'Weekly Pipeline: NOT EXECUTED', 'Final Status: ' + self.final_status)


def run_probe(*, environ, run_id, attempt, confirmed=False, client_factory=create_probe_client):
    """Live-capable probe; caller must explicitly confirm the isolated target.

    One random key per invocation. No retry/new-key fallback on uncertain PUT.
    At most 10 logical SDK calls, each with one physical attempt in our factory.
    """
    stages = dict.fromkeys(STAGES, 'NOT TESTED')
    diagnostics = []
    client = None
    owned = False
    uncertain = False
    stage = 'Configuration'
    failure = False
    calls = 0

    def call(method, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 10:
            raise ProbeError('REQUEST_BUDGET_EXHAUSTED')
        return getattr(client, method)(**kwargs)

    def readback():
        response = call('get_object', Bucket=settings.bucket, Key=key)
        try:
            status = _http(response)
            body = response['Body'].read(65537)
            if len(body) > 65536 or type(response.get('ContentLength')) is not int or response['ContentLength'] != len(body):
                raise ProbeError('CONTENT_LENGTH_MISMATCH', status)
            if body != payload or hashlib.sha256(body).hexdigest() != sha:
                raise ProbeError('SHA256_MISMATCH', status)
            if response.get('Metadata') != metadata:
                raise ProbeError('OBJECT_METADATA_MISMATCH', status)
            return status
        except (KeyError, TypeError, AttributeError):
            raise ProbeError('INVALID_OBJECT_RESPONSE') from None
        finally:
            body_stream = response.get('Body')
            if hasattr(body_stream, 'close'):
                body_stream.close()

    try:
        if confirmed is not True:
            raise ProbeError('EXPLICIT_ISOLATED_BUCKET_CONFIRMATION_REQUIRED')
        settings = ProbeSettings.from_environ(environ)
        if not re.fullmatch(r'[0-9]{1,25}', str(run_id)) or not re.fullmatch(r'[1-9][0-9]{0,5}', str(attempt)):
            raise ProbeError('INVALID_RUN_ID_OR_ATTEMPT')
        validate_sdk_contract()
        nonce = uuid4().hex
        key = f'phase7b-probe/{run_id}/{attempt}/{nonce}/probe.json'
        payload = json.dumps({'schema_version': 1, 'purpose': 'phase7b-isolated-probe',
                              'probe_id': nonce}, sort_keys=True, separators=(',', ':')).encode()
        sha = hashlib.sha256(payload).hexdigest()
        metadata = {'probe-id': nonce, 'sha256': sha}
        stages[stage] = 'SUCCESS'
        stage = 'Authentication'
        client = client_factory(settings)
        response = call('list_objects_v2', Bucket=settings.bucket, Prefix=key, MaxKeys=1)
        status = _http(response)
        stages['Authentication'] = stages['Bucket Access'] = f'SUCCESS (HTTP {status})'
        stage = 'Object PUT'
        try:
            response = call('put_object', Bucket=settings.bucket, Key=key, Body=payload,
                            Metadata=metadata, ContentType='application/json', IfNoneMatch='*')
            status = _http(response)
            owned = True
            stages[stage] = f'SUCCESS (HTTP {status})'
        except Exception as exc:
            error = safe_error(exc)
            if error.category not in ('TIMEOUT', 'NETWORK_ERROR') and not (error.status and error.status >= 500):
                raise error
            uncertain = True
            diagnostics.append('Object PUT: UNCONFIRMED; ' + error.category)
            readback()  # A positive, exact ownership read can reconcile a lost acknowledgement.
            owned = True
            uncertain = False
            stages[stage] = 'SUCCESS (same-key read verified after uncertain PUT)'
        stage = 'Object GET'
        status = readback()
        stages[stage] = f'SUCCESS (HTTP {status})'
        stages['SHA-256 Integrity'] = 'SUCCESS'
        stage = 'Conditional PUT'
        try:
            response = call('put_object', Bucket=settings.bucket, Key=key,
                            Body=b'{"purpose":"must-not-overwrite"}', Metadata=metadata,
                            ContentType='application/json', IfNoneMatch='*')
            _http(response)
        except Exception as exc:
            error = safe_error(exc)
            if error.status != 412:
                if error.category in ('TIMEOUT', 'NETWORK_ERROR') or (error.status and error.status >= 500 and error.status != 501):
                    # A pending second conditional write must not become eligible
                    # because cleanup deleted the first object before it completed.
                    uncertain = True
                    owned = False
                raise error
            readback()  # 412 alone does not prove the first object survived unchanged.
            stages[stage] = 'SUCCESS (HTTP 412; original bytes and metadata verified)'
        else:
            raise ProbeError('CONDITIONAL_PUT_OVERWRITE_NOT_BLOCKED')
    except Exception as exc:
        failure = True
        error = safe_error(exc)
        stages[stage] = ('UNCONFIRMED' if uncertain else 'FAILED') + '; ' + error.category
        if error.status is not None:
            stages[stage] += f'; HTTP {error.status}'
        if stage == 'Authentication':
            stages['Bucket Access'] = 'NOT VERIFIED'
    finally:
        if owned:
            try:
                # Never delete an existing/foreign object. Unique nonce + exact read
                # identifies this invocation's object; no prefix/bucket deletion API.
                readback()
                status = _http(call('delete_object', Bucket=settings.bucket, Key=key))
                stages['Cleanup'] = f'SUCCESS (HTTP {status})'
            except Exception as exc:
                error = safe_error(exc)
                stages['Cleanup'] = 'CLEANUP_WARNING; ' + error.category
                if error.status is not None:
                    stages['Cleanup'] += f'; HTTP {error.status}'
        elif uncertain:
            stages['Cleanup'] = 'CLEANUP_WARNING; OWNERSHIP_UNCONFIRMED; lifecycle required'
        else:
            stages['Cleanup'] = 'NOT NEEDED (no object owned)'
        if client is not None:
            try:
                client.close()
            except Exception:
                diagnostics.append('CLIENT_CLOSE_WARNING')
    warning = stages['Cleanup'].startswith('CLEANUP_WARNING') or bool(diagnostics and diagnostics[-1] == 'CLIENT_CLOSE_WARNING')
    final = 'UNCONFIRMED' if uncertain else 'FAILED' if failure else 'CLEANUP_WARNING' if warning else 'SUCCESS'
    return ProbeResult(tuple(stages.items()), tuple(diagnostics), final, 1 if failure else 2 if warning else 0)
