"""Phase 7B: no credentials, sockets, production stores or SQLite."""
from io import BytesIO
from pathlib import Path
import socket

import pytest
import yaml

from scripts.check_observation_r2 import main
from services import observation_r2_probe as probe


ROOT = Path(__file__).resolve().parents[1]
ENV = dict(zip(probe.ENV_NAMES, ('https://' + 'a'*32 + '.r2.cloudflarestorage.com',
                                'ACCESS_SECRET_SENTINEL', 'PRIVATE_SECRET_SENTINEL',
                                'gwr-observation-test-offline')))


class SDKError(Exception):
    def __init__(self, code='AccessDenied', status=403):
        super().__init__('PRIVATE_SECRET_SENTINEL Authorization ACCESS_SECRET_SENTINEL')
        self.response = {'Error': {'Code': code, 'Message': str(self)},
                         'ResponseMetadata': {'HTTPStatusCode': status}}


class FakeS3:
    def __init__(self, faults=None):
        self.faults = faults or {}
        self.objects = {}
        self.calls = []
        self.counts = {}
        self.closed = False
        self.corrupt = None

    def step(self, method, kwargs):
        self.calls.append((method, kwargs))
        assert kwargs['Bucket'].startswith('gwr-observation-test-')
        key = kwargs.get('Key', kwargs.get('Prefix'))
        assert key.startswith('phase7b-probe/')
        self.counts[method] = self.counts.get(method, 0) + 1
        effect = self.faults.get((method, self.counts[method]))
        if callable(effect):
            effect(self, kwargs)
        elif effect:
            raise effect

    def list_objects_v2(self, **kw):
        self.step('list', kw)
        return {'ResponseMetadata': {'HTTPStatusCode': 200}, 'Contents': []}

    def put_object(self, **kw):
        self.step('put', kw)
        assert kw['IfNoneMatch'] == '*'
        if kw['Key'] in self.objects:
            raise SDKError('PreconditionFailed', 412)
        self.objects[kw['Key']] = (kw['Body'], kw['Metadata'])
        return {'ResponseMetadata': {'HTTPStatusCode': 200}}

    def get_object(self, **kw):
        self.step('get', kw)
        if kw['Key'] not in self.objects:
            raise SDKError('NoSuchKey', 404)
        body, metadata = self.objects[kw['Key']]
        response = {'ResponseMetadata': {'HTTPStatusCode': 200},
                    'Body': BytesIO(body), 'ContentLength': len(body), 'Metadata': metadata,
                    'ETag': 'opaque-not-a-sha256'}
        if self.corrupt:
            self.corrupt(response)
        return response

    def delete_object(self, **kw):
        self.step('delete', kw)
        del self.objects[kw['Key']]
        return {'ResponseMetadata': {'HTTPStatusCode': 204}}

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*a, **kw):
        raise AssertionError('NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


def run(client=None, environ=None, **kw):
    client = client or FakeS3()
    result = probe.run_probe(environ=ENV if environ is None else environ,
        run_id='123', attempt='1', confirmed=True, client_factory=lambda settings: client, **kw)
    return result, client


@pytest.mark.parametrize('name', probe.ENV_NAMES)
def test_missing_each_variable_fails_before_client(name):
    env = ENV.copy(); env.pop(name)
    result, client = run(environ=env)
    assert result.exit_code == 1 and not client.calls
    assert 'MISSING_ENV: ' + name in '\n'.join(result.lines())


@pytest.mark.parametrize('bucket', ['production', 'gwr-observation-test-', 'gwr-observation-test-X',
                                    'gwr-observation-test-../x', 'gwr-observation-test-'+'a'*64])
def test_invalid_bucket_is_rejected(bucket):
    result, client = run(environ={**ENV, 'OBS_R2_BUCKET': bucket})
    assert result.exit_code == 1 and not client.calls


@pytest.mark.parametrize('endpoint', ['http://'+'a'*32+'.r2.cloudflarestorage.com',
    'https://example.com', 'https://'+'a'*32+'.r2.cloudflarestorage.com.evil.test',
    'https://user:secret@'+'a'*32+'.r2.cloudflarestorage.com',
    ENV['OBS_R2_ENDPOINT']+'?secret=hidden', ENV['OBS_R2_ENDPOINT']+'/bucket'])
def test_invalid_endpoint_never_connects(endpoint):
    result, client = run(environ={**ENV, 'OBS_R2_ENDPOINT': endpoint})
    assert result.exit_code == 1 and not client.calls
    assert 'INVALID_FORMAT: OBS_R2_ENDPOINT' in '\n'.join(result.lines())


def test_production_settings_never_used_as_fallback():
    env = {name.removeprefix('OBS_'): value for name, value in ENV.items()}
    result, client = run(environ=env)
    assert result.exit_code == 1 and not client.calls
    assert 'MISSING_ENV: OBS_R2_ENDPOINT' in '\n'.join(result.lines())


@pytest.mark.parametrize('jurisdiction', ['', '.eu', '.us', '.fedramp'])
def test_official_endpoint_forms(jurisdiction):
    env = {**ENV, 'OBS_R2_ENDPOINT': 'https://'+'a'*32+jurisdiction+'.r2.cloudflarestorage.com'}
    assert probe.ProbeSettings.from_environ(env).endpoint == env['OBS_R2_ENDPOINT']


def test_run_identity_cannot_inject_a_prefix():
    result = probe.run_probe(environ=ENV, run_id='../production', attempt='1', confirmed=True,
                             client_factory=lambda settings: pytest.fail('CLIENT_FORBIDDEN'))
    assert result.exit_code == 1 and 'INVALID_RUN_ID_OR_ATTEMPT' in '\n'.join(result.lines())


def test_success_signed_list_put_get_sha_conditional_and_owned_cleanup():
    result, client = run()
    assert result.exit_code == 0 and result.final_status == 'SUCCESS'
    assert all(value.startswith('SUCCESS') for _, value in result.stages)
    assert client.counts == {'list': 1, 'put': 2, 'get': 3, 'delete': 1}
    assert client.closed and not client.objects
    assert 'HTTP 412' in dict(result.stages)['Conditional PUT']
    assert len({kw.get('Key', kw.get('Prefix')) for _, kw in client.calls}) == 1
    assert 'Production Database: NOT ACCESSED' in result.lines()


@pytest.mark.parametrize('code,status,category', [
    ('InvalidAccessKeyId',403,'INVALID_ACCESS_KEY'), ('SignatureDoesNotMatch',403,'SIGNATURE_MISMATCH'),
    ('AccessDenied',403,'ACCESS_DENIED'), ('NoSuchBucket',404,'BUCKET_NOT_FOUND'),
    ('SlowDown',429,'RATE_LIMIT')])
def test_auth_or_bucket_error_categories(code, status, category):
    result, client = run(FakeS3({('list', 1): SDKError(code, status)}))
    assert result.exit_code == 1 and category in '\n'.join(result.lines())
    assert client.counts == {'list': 1}
    assert dict(result.stages)['Bucket Access'] == 'NOT VERIFIED'


@pytest.mark.parametrize('method', ['put', 'get'])
def test_object_request_failure(method):
    result, client = run(FakeS3({(method, 1): SDKError()}))
    assert result.exit_code == 1
    assert 'ACCESS_DENIED' in dict(result.stages)['Object '+method.upper()]


@pytest.mark.parametrize('corruption,reason', [
    (lambda r: r.update(Body=BytesIO(b'x'*r['ContentLength'])), 'SHA256_MISMATCH'),
    (lambda r: r.update(ContentLength=-1), 'CONTENT_LENGTH_MISMATCH'),
    (lambda r: r.update(Metadata={}), 'OBJECT_METADATA_MISMATCH'),
    (lambda r: r.update(ResponseMetadata={}), 'INVALID_HTTP_RESPONSE')])
def test_readback_corruption_never_success(corruption, reason):
    client = FakeS3(); client.corrupt = corruption
    result, client = run(client)
    assert result.exit_code == 1 and reason in '\n'.join(result.lines())
    assert client.counts.get('delete', 0) == 0  # Ownership cannot be revalidated.


def test_sdk_conditional_contract_unsupported_before_client(monkeypatch):
    def unsupported():
        raise probe.ProbeError('CONDITIONAL_PUT_NOT_SUPPORTED')
    monkeypatch.setattr(probe, 'validate_sdk_contract', unsupported)
    result, client = run()
    assert result.exit_code == 1 and not client.calls
    assert 'CONDITIONAL_PUT_NOT_SUPPORTED' in '\n'.join(result.lines())


def test_real_bundled_sdk_model_supports_if_none_match_offline():
    probe.validate_sdk_contract()


def test_initial_412_is_existing_foreign_object_no_cleanup():
    result, client = run(FakeS3({('put', 1): SDKError('PreconditionFailed', 412)}))
    assert result.exit_code == 1
    assert not client.counts.get('delete') and not client.counts.get('get')


def test_conditional_put_unsupported_on_server():
    result, client = run(FakeS3({('put', 2): SDKError('NotImplemented', 501)}))
    assert result.exit_code == 1 and 'CONDITIONAL_PUT_NOT_SUPPORTED' in '\n'.join(result.lines())
    assert client.counts['delete'] == 1


def test_conditional_second_put_incorrectly_overwrites_rejected():
    def erase(client, kw):
        client.objects.pop(kw['Key'])
    result, client = run(FakeS3({('put', 2): erase}))
    assert result.exit_code == 1
    assert 'CONDITIONAL_PUT_OVERWRITE_NOT_BLOCKED' in '\n'.join(result.lines())
    assert client.counts.get('delete', 0) == 0


def test_412_but_original_corrupted_is_failure():
    def corrupt(client, kw):
        client.objects[kw['Key']] = (b'changed', kw['Metadata'])
        raise SDKError('PreconditionFailed', 412)
    result, client = run(FakeS3({('put', 2): corrupt}))
    assert result.exit_code == 1 and 'SHA256_MISMATCH' in '\n'.join(result.lines())


def test_timeout_after_commit_reconciles_same_key():
    def commit_then_timeout(client, kw):
        client.objects[kw['Key']] = (kw['Body'], kw['Metadata'])
        raise TimeoutError('PRIVATE_SECRET_SENTINEL')
    result, client = run(FakeS3({('put', 1): commit_then_timeout}))
    assert result.exit_code == 0
    assert client.counts['put'] == 2 and client.counts['get'] == 4
    assert len(client.calls) <= 10


@pytest.mark.parametrize('error', [TimeoutError('secret'), OSError('secret')])
def test_uncertain_put_absent_read_is_not_proof_of_failure(error):
    result, client = run(FakeS3({('put', 1): error}))
    assert result.final_status == 'UNCONFIRMED' and result.exit_code == 1
    assert client.counts == {'list': 1, 'put': 1, 'get': 1}
    assert 'OWNERSHIP_UNCONFIRMED' in dict(result.stages)['Cleanup']


@pytest.mark.parametrize('error,category', [(TimeoutError('secret'),'TIMEOUT'), (OSError('secret'),'NETWORK_ERROR')])
def test_network_and_timeout_on_auth(error, category):
    result, client = run(FakeS3({('list', 1): error}))
    assert result.exit_code == 1 and category in '\n'.join(result.lines())


def test_cleanup_denied_is_warning_not_complete_success():
    result, client = run(FakeS3({('delete', 1): SDKError()}))
    assert result.exit_code == 2 and result.final_status == 'CLEANUP_WARNING'
    assert client.objects


def test_uncertain_second_put_does_not_delete_original_under_pending_request():
    result, client = run(FakeS3({('put', 2): TimeoutError('secret')}))
    assert result.final_status == 'UNCONFIRMED' and result.exit_code == 1
    assert client.objects and client.counts.get('delete', 0) == 0
    assert 'CLEANUP_WARNING' in dict(result.stages)['Cleanup']


def test_missing_explicit_confirmation_does_not_read_environment_or_create_client():
    class Forbidden(dict):
        def get(self, *args):
            raise AssertionError('ENV_READ_FORBIDDEN')
    result = probe.run_probe(environ=Forbidden(), run_id='1', attempt='1',
        client_factory=lambda settings: pytest.fail('CLIENT_FORBIDDEN'))
    assert 'EXPLICIT_ISOLATED_BUCKET_CONFIRMATION_REQUIRED' in '\n'.join(result.lines())


def test_safe_logs_contain_no_secret_details():
    lines = []
    code = main(['--mode','live','--confirm-isolated-bucket'], environ=ENV,
                client_factory=lambda settings: FakeS3({('list',1): SDKError()}), emit=lines.append)
    text = '\n'.join(lines)
    assert code == 1
    for value in ENV.values():
        assert value not in text
    assert 'Authorization' not in text and 'ACCESS_DENIED' in text
    assert all(value not in repr(probe.ProbeSettings.from_environ(ENV)) for value in ENV.values())


def test_validate_never_reads_env_or_creates_client(monkeypatch, tmp_path):
    class Forbidden(dict):
        def get(self, *a): pytest.fail('ENV_FORBIDDEN')
    import boto3
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('CLIENT_FORBIDDEN'))
    summary = tmp_path/'summary.md'
    lines = []
    assert main(['--summary', str(summary)], environ=Forbidden(), emit=lines.append) == 0
    assert 'NOT TESTED' in summary.read_text()
    assert 'OFFLINE VALIDATION SUCCESS' in '\n'.join(lines)


def test_client_factory_has_single_attempt_no_default_credential_chain(monkeypatch):
    import boto3
    captured = {}
    def fake(service, **kw):
        captured.update(kw)
        return object()
    monkeypatch.setattr(boto3, 'client', fake)
    probe.create_probe_client(probe.ProbeSettings.from_environ(ENV))
    assert captured['config'].retries['total_max_attempts'] == 1
    assert captured['aws_access_key_id'] == ENV['OBS_R2_ACCESS_KEY_ID']
    assert captured['endpoint_url'] == ENV['OBS_R2_ENDPOINT']


def workflow():
    return yaml.safe_load((ROOT/'.github/workflows/observation-r2-check.yml').read_text(encoding='utf-8'))


def test_workflow_manual_modes_explicit_confirm_permissions_concurrency():
    data = workflow()
    assert set(data['on']) == {'workflow_dispatch'} and True not in data
    inputs = data['on']['workflow_dispatch']['inputs']
    assert inputs['mode']['default'] == 'validate' and inputs['mode']['options'] == ['validate','live']
    assert inputs['confirm_isolated_bucket']['default'] is False
    assert inputs['confirm_isolated_bucket']['type'] == 'boolean'
    assert data['permissions'] == {'contents': 'read'}
    assert data['concurrency'] == {'group':'github-weekly-report-observation-r2-check',
                                   'cancel-in-progress':False,'queue':'max'}
    for name in ('observation.yml','weekly-report.yml'):
        old = yaml.safe_load((ROOT/'.github/workflows'/name).read_text(encoding='utf-8'))
        assert old['concurrency']['group'] != data['concurrency']['group']


def test_workflow_validate_has_no_secrets_live_only_environment_obs_secrets():
    jobs = workflow()['jobs']
    validate = jobs['validate']; live = jobs['live']
    assert 'secrets.' not in str(validate) and 'environment' not in validate
    assert "inputs.mode == 'validate'" in validate['if']
    assert "inputs.mode == 'live'" in live['if']
    assert live['environment'] == 'observation-r2-test'
    assert live['steps'][0]['run'].count('exit 1') == 1
    assert 'inputs.confirm_isolated_bucket' in live['steps'][0]['env']['CONFIRMED']
    env = live['steps'][-1]['env']
    assert set(env) == set(probe.ENV_NAMES)
    assert all(env[name] == '${{ secrets.'+name+' }}' for name in probe.ENV_NAMES)
    assert '--confirm-isolated-bucket' in live['steps'][-1]['run']
    for job in jobs.values():
        assert job['runs-on'] == 'ubuntu-latest' and job['timeout-minutes'] <= 20
        assert not job.get('continue-on-error')
        assert any(s.get('with',{}).get('python-version') == '3.11' for s in job['steps'])
        assert all(not s.get('continue-on-error') for s in job['steps'])
        assert not any('upload-artifact' in s.get('uses','') for s in job['steps'])


def test_probe_has_no_production_or_report_dependencies():
    source = (ROOT/'services/observation_r2_probe.py').read_text()
    workflow_text = str(workflow())
    for forbidden in ('run_weekly_live_once', 'r2_persistence', 'weekly_live_runner',
                      'sqlite3', 'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'secrets.R2_', 'GH_API_TOKEN'):
        assert forbidden not in source and forbidden not in workflow_text
    assert 'delete_objects(' not in source and 'delete_bucket(' not in source
