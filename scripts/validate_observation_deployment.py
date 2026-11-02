"""Offline structure checks only. Never load credentials or create API clients."""
from contextlib import ExitStack
import argparse
from dataclasses import fields, is_dataclass
from importlib import import_module
import inspect
from pathlib import Path
import sys
from unittest.mock import patch

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ('httpx','dotenv','tzdata','pytest','boto3','botocore','yaml','sqlite3')
MODULES = (
    'config.settings','config.observation_rules','models.observation','models.observation_storage',
    'models.observation_context','services.observation_pool','services.observation_validation',
    'services.observation_pool_validation','services.r2_observation_store','services.observation_job',
    'services.observation_consumer','services.observation_pipeline','services.pipeline',
)
INTERFACES = (
    ('services.observation_pool','build_pool',('records','as_of','fixture')),
    ('services.observation_validation','validate_observation',('value','now','namespace')),
    ('services.observation_pool_validation','validate_pool',('value','now','namespace')),
    ('services.observation_job','run_boundary_observation',('store','github','producer','clock','observation_id')),
    ('services.observation_consumer','consume_observations',('store','period','cutoff','repository_ids')),
    ('services.observation_pipeline','production_observation_loader',('settings',)),
    ('services.pipeline','ReportPipeline',('database','github','web','observation_loader')),
)
MODELS = (
    ('models.observation','ObservationEnvelope',('schema_version','payload','integrity')),
    ('models.observation','ObservationPayload',('mode','fixture','observation_id','period_start','boundary_end',
        'started_at','completed_at','producer','repositories','trending','pool_ref','collection_status','boundary_coverage')),
    ('models.observation','RepositoryObservation',('github_id','metadata','stars','forks','source_url','status',
        'request_started_at','response_received_at','observed_at')),
    ('models.observation','TrendingObservation',('entries','page_sha256','source_url','status','observed_at')),
    ('models.observation_storage','PoolEnvelope',('schema_version','payload','integrity')),
    ('models.observation_context','ObservationContext',('namespace','period_start','period_end','cutoff',
        'repositories','trending','coverage','diagnostics','summary')),
)
STORE_METHODS = ('publish_observation','publish_pool','get_observation','get_pool','list_observations','list_recent_pools')
SOURCE_FILES = tuple(name.replace('.','/')+'.py' for name in MODULES)
REQUIRED_FILES = (*SOURCE_FILES,'requirements.txt','.github/workflows/observation.yml',
                  'tests/test_observation_e2e.py','scripts/run_boundary_observation_once.py')


class OfflineBoundaryError(RuntimeError):
    pass


def _forbidden(*args,**kwargs):
    raise OfflineBoundaryError('SIDE_EFFECT_FORBIDDEN')


def validate_observation_deployment(*, root=ROOT, module_loader=import_module,
                                    python_version=None, emit=print):
    """Check installed code/configuration contracts, not credentials or services.

    Category/name output is fixed; arbitrary exception text is never printed.
    Compile uses memory only, avoiding pyc writes or touching local SQLite state.
    """
    def fail(category):
        emit('Observation deployment validation: FAILED; '+category)
        return 1
    if tuple(python_version or sys.version_info[:2]) != (3,11):
        return fail('PYTHON_3_11_REQUIRED')
    root = Path(root)
    for name in REQUIRED_FILES:
        if not (root/name).is_file(): return fail('MISSING_FILE: '+name)
    try:
        # Install hard I/O guards before dependency/application imports.
        with ExitStack() as guards:
            for target in ('socket.create_connection','socket.socket.connect','sqlite3.connect'):
                guards.enter_context(patch(target,side_effect=_forbidden))
            loaded = {}
            for name in DEPENDENCIES:
                try: loaded[name] = module_loader(name)
                except Exception: return fail('DEPENDENCY_IMPORT_FAILED: '+name)
            for target in ('httpx.Client.__init__','httpx.AsyncClient.__init__',
                           'boto3.client','boto3.session.Session.client'):
                guards.enter_context(patch(target,side_effect=_forbidden))
            try:
                loaded['config.settings'] = module_loader('config.settings')
                settings_type = loaded['config.settings'].Settings
                guards.enter_context(patch.object(settings_type,'load',side_effect=_forbidden))
                for name in MODULES:
                    if name not in loaded: loaded[name] = module_loader(name)
            except Exception:
                return fail('OBSERVATION_MODULE_IMPORT_FAILED')
            for module,name,required in MODELS:
                model = getattr(loaded[module],name,None)
                if (not is_dataclass(model) or not model.__dataclass_params__.frozen
                        or not set(required) <= {f.name for f in fields(model)}):
                    return fail('MODEL_CONTRACT: '+name)
            for module,name,parameters in INTERFACES:
                obj = getattr(loaded[module],name,None)
                if not callable(obj) or not set(parameters) <= set(inspect.signature(obj).parameters):
                    return fail('INTERFACE_CONTRACT: '+name)
            store = loaded['services.r2_observation_store'].R2ObservationStore
            if any(not callable(getattr(store,name,None)) for name in STORE_METHODS):
                return fail('STORE_INTERFACE_CONTRACT')
            for name,required in (
                ('get_observation',{'boundary','observation_id'}),('get_pool',{'boundary','pool_id'}),
                ('publish_observation',{'envelope'}),('publish_pool',{'envelope'})):
                if not required <= set(inspect.signature(getattr(store,name)).parameters):
                    return fail('STORE_INTERFACE_CONTRACT')
            parameters = inspect.signature(store).parameters
            if not {'settings','as_of','namespace','profile','client'} <= set(parameters):
                return fail('STORE_CONTEXT_CONTRACT')
            if not is_dataclass(settings_type): return fail('SETTINGS_STRUCTURE')
            settings_fields = {f.name for f in fields(settings_type)}
            if not {'github_token','timezone','r2_endpoint','r2_access_key_id','r2_secret_access_key','r2_bucket'} <= settings_fields:
                return fail('SETTINGS_STRUCTURE')
            # Dataclass defaults only. Never Settings.load(), os.environ or .env.
            if settings_type().timezone != 'Asia/Seoul': return fail('TIMEZONE_DEFAULT')
            rules = loaded['config.observation_rules']
            caps = {'POOL_LIMIT':40,'MAX_JSON_BYTES':1024*1024,'JOB_DEADLINE_SECONDS':480,
                    'METADATA_REQUEST_LIMIT':40,'REST_ATTEMPT_LIMIT':45,'TRENDING_ATTEMPT_LIMIT':2}
            if (type(rules.SCHEMA_VERSION) is not int or rules.SCHEMA_VERSION != 1 or
                    any(type(getattr(rules,k)) is not int or not 0 < getattr(rules,k) <= cap for k,cap in caps.items()) or
                    not {'publication','observation_job','report_read'} <= set(rules.R2_STORE_BUDGETS)):
                return fail('OBSERVATION_RULES_STRUCTURE')
            for name in SOURCE_FILES:
                try: compile((root/name).read_bytes(),'<observation-source>','exec')
                except Exception: return fail('PYTHON_COMPILE_FAILED: '+name)
    except Exception:
        return fail('STRUCTURE_OR_OFFLINE_BOUNDARY_FAILED')
    emit('Observation modules/models/pool/validator/store/job/consumer/pipeline: PASS')
    emit('Configuration structure/dependencies/Python 3.11/in-memory compile: PASS')
    emit('Secrets not loaded; no API clients, service calls or SQLite access')
    emit('R2 authentication/connectivity/conditional PUT: NOT TESTED')
    emit('Observation deployment validation: PASS (offline only)')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Observation deployment validation (offline only)')
    parser.parse_args(argv)
    return validate_observation_deployment()


if __name__ == '__main__':
    raise SystemExit(main())
