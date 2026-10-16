from dataclasses import replace
from importlib import import_module
from pathlib import Path
import re

import pytest
import yaml

from config.settings import Settings
from scripts.validate_weekly_deployment import REQUIRED_FILES, REQUIRED_IMPORTS, validate_deployment


WORKFLOW_PATH = Path(__file__).resolve().parents[1]/'.github/workflows/weekly-report.yml'


def workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding='utf-8'))


def steps_by_id():
    return {step['id']:step for step in workflow()['jobs']['weekly-report']['steps'] if 'id' in step}


def test_workflow_valid_yaml_and_default_dispatch_is_validate_only():
    parsed = workflow()
    assert set(parsed['on']) == {'workflow_dispatch'}  # Schedule deliberately inactive at this stage.
    selection = parsed['on']['workflow_dispatch']['inputs']['mode']
    assert selection == {
        'description':'validate checks configuration offline; production sends a real report',
        'required':True,'default':'validate','type':'choice','options':['validate','production']}
    assert 'push' not in parsed['on'] and 'pull_request' not in parsed['on']


def test_disabled_schedule_has_exact_kst_sunday_1900_rule():
    lines = WORKFLOW_PATH.read_text(encoding='utf-8').splitlines()
    index = next(index for index,line in enumerate(lines) if line.strip() == '# schedule:')
    block = '\n'.join(re.sub(r'^  # ?', '',line) for line in lines[index:index+3])
    assert yaml.safe_load(block) == {
        'schedule':[{'cron':'0 19 * * 0','timezone':'Asia/Seoul'}]}


def test_workflow_serializes_entire_run_without_canceling_pending_runs():
    assert workflow()['concurrency'] == {
        'group':'github-weekly-report-production','cancel-in-progress':False,'queue':'max'}


def test_workflow_uses_python311_ubuntu_root_entrypoint_and_bounded_timeout():
    job = workflow()['jobs']['weekly-report']
    assert job['runs-on'] == 'ubuntu-latest'
    assert job['timeout-minutes'] == 60
    checkout = next(step for step in job['steps'] if step.get('uses','').startswith('actions/checkout@'))
    assert checkout['with']['persist-credentials'] is False
    setup = next(step for step in job['steps'] if step.get('uses','').startswith('actions/setup-python@'))
    assert setup['with']['python-version'] == '3.11'
    assert setup['with']['cache'] == 'pip'
    assert setup['with']['cache-dependency-path'] == 'requirements.txt'
    assert workflow()['permissions'] == {'contents':'read'}
    steps = steps_by_id()
    assert 'python -m pip install -r requirements.txt' in steps['dependencies']['run']
    assert 'python -m pip check' in steps['dependencies']['run']
    assert steps['production']['run'] == 'python scripts/run_weekly_live_once.py'
    # Entry point inserts repository root for package imports without PYTHONPATH tricks.
    from scripts import run_weekly_live_once
    assert callable(run_weekly_live_once.main)


def test_secrets_exactly_match_settings_and_only_exist_on_relevant_steps(tmp_path):
    expected = {
        'GITHUB_TOKEN':'GH_API_TOKEN', 'OPENAI_API_KEY':'OPENAI_API_KEY',
        'TELEGRAM_BOT_TOKEN':'TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID':'TELEGRAM_CHAT_ID',
        'R2_ENDPOINT':'R2_ENDPOINT','R2_ACCESS_KEY_ID':'R2_ACCESS_KEY_ID',
        'R2_SECRET_ACCESS_KEY':'R2_SECRET_ACCESS_KEY','R2_BUCKET':'R2_BUCKET',
    }
    steps = steps_by_id()
    for name in ('configuration','production'):
        assert steps[name]['env'] == {key:'${{ secrets.'+secret+' }}' for key,secret in expected.items()}
    assert '${{ secrets.' not in str(workflow().get('env',{}))
    assert '${{ secrets.' not in str(workflow()['jobs']['weekly-report']['env'])
    assert '${{ secrets.' not in str(steps['dependencies'])
    assert '${{ secrets.' not in str(steps['tests'])
    injected = {key:'dummy-'+secret for key,secret in expected.items()}
    injected['R2_ENDPOINT'] = 'https://example.r2.cloudflarestorage.com'
    injected.update(workflow()['jobs']['weekly-report']['env'])
    configured = Settings.load(tmp_path/'absent.env',environ=injected)
    configured.validate_live(send=True)
    configured.validate_r2()
    assert configured.github_token == 'dummy-GH_API_TOKEN'
    assert configured.database_path == 'data/github_weekly_report.db'
    assert configured.timezone == 'Asia/Seoul'


def test_production_step_is_explicitly_gated_and_exit_failure_is_not_masked():
    steps = steps_by_id()
    assert steps['production']['if'] == (
        "${{ github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.mode == 'production') }}")
    assert steps['tests']['if'] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.mode == 'validate' }}")
    assert steps['configuration']['run'] == 'python scripts/validate_weekly_deployment.py'
    parsed = workflow()
    job = parsed['jobs']['weekly-report']
    assert not job.get('continue-on-error',False)
    assert not job.get('env',{}).get('PYTHONPATH')
    assert parsed['defaults']['run']['shell'] == 'bash'
    for step in job['steps']:
        assert not step.get('continue-on-error',False)
        command = step.get('run','')
        assert '|| true' not in command
        assert 'exit 0' not in command
        assert '--allow-initial-bootstrap' not in command
        assert '--integration-test' not in command and '--dry-run' not in command
        assert 'main.py schedule' not in command and 'scheduler_loop' not in command


def test_diagnostics_never_upload_database_or_raw_files():
    steps = workflow()['jobs']['weekly-report']['steps']
    assert not any('upload-artifact' in step.get('uses','') for step in steps)
    summary = steps[-1]
    assert summary['if'] == '${{ always() }}'
    assert all('.outcome }}' in value for value in summary['env'].values())
    assert 'GITHUB_STEP_SUMMARY' in summary['run']
    assert 'cat ' not in summary['run'] and 'printenv' not in summary['run']
    assert '.env' not in summary['run'] and 'secrets.' not in summary['run']


def deployment_fixture(tmp_path):
    for name in REQUIRED_FILES:
        file = tmp_path/name
        file.parent.mkdir(parents=True,exist_ok=True)
        file.touch()
    return Settings(github_token='dummy-github',openai_api_key='dummy-openai',
                    telegram_bot_token='dummy-telegram',telegram_chat_id='dummy-chat',
                    r2_endpoint='https://example.r2.cloudflarestorage.com',
                    r2_access_key_id='dummy-access',r2_secret_access_key='dummy-secret',r2_bucket='dummy-bucket')


def test_validate_configuration_has_no_api_calls_or_db_side_effects(tmp_path,monkeypatch,capsys):
    configured = deployment_fixture(tmp_path)
    from database.db import Database
    from services.pipeline import ReportPipeline
    from services.r2_persistence import R2DatabaseStore
    def forbidden(*args,**kwargs):
        pytest.fail('Offline validator attempted a production operation')
    monkeypatch.setattr(Database,'__init__',forbidden)
    monkeypatch.setattr(ReportPipeline,'run',forbidden)
    monkeypatch.setattr(R2DatabaseStore,'__init__',forbidden)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob('*'))
    imported = []
    def load(name):
        imported.append(name)
        # boto3 is not installed in the sandbox. SDK imports are stubbed without skip;
        # the production modules themselves really import, with all mutators forbidden.
        return object() if name in ('boto3','botocore') else import_module(name)
    assert validate_deployment(configured,root=tmp_path,module_loader=load,python_version=(3,11)) == 0
    assert imported == list(REQUIRED_IMPORTS)
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob('*')) == before
    text = capsys.readouterr().out
    assert 'PASS (offline only)' in text
    assert 'dummy-' not in text


@pytest.mark.parametrize('change',[
    {'github_token':''},{'r2_secret_access_key':''},{'timezone':'UTC'},
    {'database_path':'data/full_integration_test.db'},
    {'r2_endpoint':'https://user:sensitive-password@host'},
])
def test_validate_invalid_config_fails_without_mutation_or_secret_output(tmp_path,change,capsys):
    configured = replace(deployment_fixture(tmp_path),**change)
    assert validate_deployment(configured,root=tmp_path,module_loader=lambda _:object(),python_version=(3,11)) == 1
    assert not (tmp_path/'data/github_weekly_report.db').exists()
    assert 'sensitive-password' not in capsys.readouterr().out


def test_validate_preserves_existing_local_db_and_never_opens_it(tmp_path):
    configured = deployment_fixture(tmp_path)
    path = tmp_path/'data/github_weekly_report.db'
    path.parent.mkdir()
    path.write_bytes(b'unchanged local database')
    assert validate_deployment(configured,root=tmp_path,module_loader=lambda _:object(),python_version=(3,11)) == 1
    assert path.read_bytes() == b'unchanged local database'


def test_validate_missing_dependency_fails_without_printing_raw_error(tmp_path,capsys):
    configured = deployment_fixture(tmp_path)
    def unavailable(name):
        raise ImportError('private endpoint and secret details')
    assert validate_deployment(configured,root=tmp_path,module_loader=unavailable,python_version=(3,11)) == 1
    text = capsys.readouterr().out
    assert 'dependency import unavailable' in text
    assert 'private endpoint' not in text


def test_validate_rejects_wrong_python_or_missing_project_files(tmp_path):
    configured = deployment_fixture(tmp_path)
    assert validate_deployment(configured,root=tmp_path,python_version=(3,12)) == 1
    (tmp_path/'scripts/run_weekly_live_once.py').unlink()
    assert validate_deployment(configured,root=tmp_path,python_version=(3,11)) == 1
