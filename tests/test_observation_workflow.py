"""Static Actions contract and side-effect-free deployment validator checks."""
import hashlib
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import validate_observation_deployment as validator


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT/'.github/workflows/observation.yml'
WEEKLY = ROOT/'.github/workflows/weekly-report.yml'
WEEKLY_SHA256 = '0552628a7319ef4b80004569dde3afc775cd62499c5bb576674e31a5b4282ea0'


def workflow(): return yaml.safe_load(PATH.read_text(encoding='utf-8'))
def steps(): return {s['id']:s for s in workflow()['jobs']['validate']['steps'] if 'id' in s}


def test_manual_validate_only_workflow_valid_yaml_and_quoted_on():
    data = workflow()
    assert data['name'] == 'Observation System'
    assert True not in data and set(data['on']) == {'workflow_dispatch'}
    mode = data['on']['workflow_dispatch']['inputs']['mode']
    assert mode['type'] == 'choice' and mode['required'] is True
    assert mode['default'] == 'validate' and mode['options'] == ['validate']
    assert set(data['jobs']) == {'validate'}
    assert data['jobs']['validate']['if'] == "${{ github.event_name == 'workflow_dispatch' && inputs.mode == 'validate' }}"


def test_observation_permissions_and_concurrency_are_isolated():
    data = workflow()
    weekly = yaml.safe_load(WEEKLY.read_text(encoding='utf-8'))
    assert data['permissions'] == {'contents':'read'}
    assert 'permissions' not in data['jobs']['validate']
    assert data['concurrency'] == {'group':'github-weekly-report-observation',
                                   'cancel-in-progress':False,'queue':'max'}
    assert data['concurrency']['group'].lower() != weekly['concurrency']['group'].lower()


def test_ubuntu_python_dependency_actions_match_existing_workflow():
    data = workflow(); job = data['jobs']['validate']
    assert job['runs-on'] == 'ubuntu-latest' and job['timeout-minutes'] == 20
    uses = {s['uses'].split('@')[0]:s for s in job['steps'] if 'uses' in s}
    weekly = yaml.safe_load(WEEKLY.read_text(encoding='utf-8'))
    old = {s['uses'].split('@')[0]:s['uses'] for s in weekly['jobs']['weekly-report']['steps'] if 'uses' in s}
    for name,step in uses.items(): assert step['uses'] == old[name]
    assert uses['actions/checkout']['with']['persist-credentials'] is False
    assert uses['actions/setup-python']['with'] == {
        'python-version':'3.11','cache':'pip','cache-dependency-path':'requirements.txt'}
    assert 'python -m pip install -r requirements.txt' in steps()['dependencies']['run']
    assert 'python -m pip check' in steps()['dependencies']['run']


def test_commands_are_exclusively_offline_validation_and_tests():
    expected = {
        'validation':'python scripts/validate_observation_deployment.py',
        'observation_tests':'python -m pytest tests/test_observation_*.py tests/test_r2_observations.py -q',
        'tests':'python -m pytest -q',
        'compile':'python -m compileall -q config collectors models services database scripts tests',
        'diff':'git diff --check',
    }
    for key,value in expected.items(): assert steps()[key]['run'] == value
    assert list(steps()) == ['dependencies','validation','observation_tests','tests','compile','diff']
    data = workflow(); job = data['jobs']['validate']
    assert not job.get('continue-on-error')
    assert data['defaults']['run']['shell'] == 'bash'
    for step in job['steps']:
        assert not step.get('continue-on-error')
        for forbidden in ('|| true','exit 0','run_weekly_live_once','run_full_top5_live',
                          'run_boundary_observation_once','bootstrap_r2','main.py','sendMessage'):
            assert forbidden not in step.get('run','')
        if step is not job['steps'][-1]: assert 'if' not in step


def test_no_service_secrets_or_production_environment_transmitted():
    text = PATH.read_text(encoding='utf-8')
    assert 'secrets.' not in text and 'github.token' not in text
    data = workflow()
    assert 'env' not in data
    assert data['jobs']['validate']['env'] == {'PYTHONUNBUFFERED':'1','PYTHONUTF8':'1'}
    for step in data['jobs']['validate']['steps']:
        if 'env' in step:
            assert step is data['jobs']['validate']['steps'][-1]
            assert all(v.startswith('${{ steps.') and v.endswith('.outcome }}') for v in step['env'].values())
    for key in ('R2_ENDPOINT','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET',
                'OPENAI_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','GITHUB_TOKEN','DATABASE_PATH'):
        assert key not in text


def test_summary_is_safe_always_runs_and_artifacts_are_disabled():
    data = workflow(); job = data['jobs']['validate']; summary = job['steps'][-1]
    assert summary['if'] == '${{ always() }}'
    command = summary['run']
    for phrase in ('Observation System','workflow_dispatch','Python target: 3.11',
                   'Offline validation','Observation tests','Complete tests','Compile','Diff check','NOT EXECUTED'):
        assert phrase in command
    assert 'GITHUB_STEP_SUMMARY' in command
    assert not any('upload-artifact' in s.get('uses','') for s in job['steps'])
    for forbidden in ('cat ','tee ','data/','.env','r2.cloudflarestorage','artifacts/','logs/'):
        assert forbidden not in command


def test_existing_weekly_workflow_bytes_unchanged_after_line_ending_normalization():
    assert hashlib.sha256(WEEKLY.read_bytes().replace(b'\r\n',b'\n')).hexdigest() == WEEKLY_SHA256


def test_validator_success_without_settings_loading_network_clients_or_db(monkeypatch):
    def forbidden(*a,**kw): pytest.fail('Credential/DB/client access forbidden')
    monkeypatch.setattr('config.settings.Settings.load',forbidden)
    monkeypatch.setattr('sqlite3.connect',forbidden)
    monkeypatch.setattr('services.r2_observation_store.create_r2_client',forbidden)
    monkeypatch.setattr('services.r2_persistence.create_r2_client',forbidden)
    output = []
    assert validator.validate_observation_deployment(emit=output.append) == 0
    assert 'PASS (offline only)' in output[-1]
    assert any('NOT TESTED' in line for line in output)


@pytest.mark.parametrize('version',[(3,10),(3,12)])
def test_python_version_failure_is_nonzero_and_safe(version):
    output = []
    assert validator.validate_observation_deployment(python_version=version,emit=output.append) == 1
    assert output == ['Observation deployment validation: FAILED; PYTHON_3_11_REQUIRED']


def test_missing_dependency_does_not_echo_exception_or_credentials():
    def loader(name):
        if name == 'boto3': raise RuntimeError('do-not-print-sensitive-endpoint')
        return import_module(name)
    output = []
    assert validator.validate_observation_deployment(module_loader=loader,emit=output.append) == 1
    assert output == ['Observation deployment validation: FAILED; DEPENDENCY_IMPORT_FAILED: boto3']


def test_application_import_side_effect_is_blocked_before_sqlite_opens():
    def loader(name):
        if name == 'services.observation_job':
            import sqlite3
            sqlite3.connect(':memory:')  # Must be forbidden by the validator itself.
        return import_module(name)
    output = []
    assert validator.validate_observation_deployment(module_loader=loader,emit=output.append) == 1
    assert output == ['Observation deployment validation: FAILED; OBSERVATION_MODULE_IMPORT_FAILED']


@pytest.mark.parametrize('target',['model','interface','settings','rules'])
def test_contract_drift_is_detected_without_service_calls(target):
    def loader(name):
        real = import_module(name)
        if target == 'model' and name == 'models.observation':
            return SimpleNamespace(**{**vars(real),'ObservationEnvelope':dict})
        if target == 'interface' and name == 'services.observation_pool':
            return SimpleNamespace(**{**vars(real),'build_pool':lambda:None})
        if target == 'settings' and name == 'config.settings':
            class Config: load = None
            return SimpleNamespace(Settings=Config)
        if target == 'rules' and name == 'config.observation_rules':
            return SimpleNamespace(**{**vars(real),'POOL_LIMIT':1000})
        return real
    assert validator.validate_observation_deployment(module_loader=loader,emit=lambda _:None) == 1


def test_missing_source_and_syntax_error_fail_safely(tmp_path):
    output = []
    assert validator.validate_observation_deployment(root=tmp_path,emit=output.append) == 1
    assert 'MISSING_FILE' in output[-1]
    for name in validator.REQUIRED_FILES:
        destination = tmp_path/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((ROOT/name).read_bytes())
    (tmp_path/validator.SOURCE_FILES[-1]).write_text('not valid python !',encoding='utf-8')
    output.clear()
    assert validator.validate_observation_deployment(root=tmp_path,emit=output.append) == 1
    assert 'PYTHON_COMPILE_FAILED' in output[-1]


def test_validator_has_no_production_cli_option():
    with pytest.raises(SystemExit) as raised: validator.main(['--production'])
    assert raised.value.code == 2
