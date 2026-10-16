"""Offline deployment checks only: no DB opening, R2 client, collector or sender."""
from importlib import import_module
from pathlib import Path
import sys

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from config.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    'requirements.txt', 'config/settings.py', 'database/schema.sql',
    'database/sqlite_backup.py', 'services/r2_persistence.py',
    'services/weekly_live_runner.py', 'scripts/run_weekly_live_once.py',
    '.github/workflows/weekly-report.yml',
)
REQUIRED_IMPORTS = ('httpx','dotenv','boto3','botocore','yaml','sqlite3',
                    'services.weekly_live_runner','services.r2_persistence')
LIVE_FIELDS = ('github_token','openai_api_key','telegram_bot_token','telegram_chat_id')
R2_FIELDS = ('r2_endpoint','r2_access_key_id','r2_secret_access_key','r2_bucket')


def configuration_diagnostics(settings, root):
    """Return constant category/field names only, never values or exception text.

    Settings owns live/R2 policy. These checks annotate that policy and the existing
    deployment path constraints; they do not relax or normalize configuration.
    """
    diagnostics = []
    missing = {field for field in (*LIVE_FIELDS,*R2_FIELDS) if not getattr(settings,field)}
    for field in (*LIVE_FIELDS,*R2_FIELDS):
        if field in missing:
            diagnostics.append('MISSING_ENV: '+field.upper())
    if not missing.intersection(LIVE_FIELDS):
        try:
            settings.validate_live(send=True)
        except Exception:
            diagnostics.append('SETTINGS_VALIDATION_FAILED: LIVE_SETTINGS')
    if not missing.intersection(R2_FIELDS):
        try:
            settings.validate_r2()
        except ValueError:
            # Once the four fields are present, existing validate_r2() only checks
            # endpoint format (including urlsplit errors such as malformed IPv6).
            diagnostics.append('INVALID_FORMAT: R2_ENDPOINT')
        except Exception:
            diagnostics.append('SETTINGS_VALIDATION_FAILED: R2_SETTINGS')
    expected_path = root/'data/github_weekly_report.db'
    if not settings.database_path:
        diagnostics.append('MISSING_ENV: DATABASE_PATH')
    else:
        try:
            path = Path(settings.database_path)
            path = path if path.is_absolute() else root/path
            if path.resolve() != expected_path.resolve():
                diagnostics.append('INVALID_FORMAT: DATABASE_PATH')
        except Exception:
            diagnostics.append('INVALID_FORMAT: DATABASE_PATH')
    if settings.timezone != 'Asia/Seoul':
        diagnostics.append('INVALID_FORMAT: TIMEZONE')
    try:
        for suffix,category in (('', 'LOCAL_DB_EXISTS'),('-wal','LOCAL_WAL_EXISTS'),
                                ('-shm','LOCAL_SHM_EXISTS'),('-journal','LOCAL_JOURNAL_EXISTS')):
            if Path(str(expected_path)+suffix).exists():
                diagnostics.append(category+': DATABASE_PATH')
    except Exception:
        diagnostics.append('SETTINGS_VALIDATION_FAILED: LOCAL_STATE_CHECK')
    return diagnostics


def validate_deployment(settings, *, root=ROOT, module_loader=import_module,
                        python_version=None, emit=print):
    """Validate deployment configuration without testing credentials against services."""
    if tuple(python_version or sys.version_info[:2]) != (3,11):
        emit('Deployment validation: FAILED; Python 3.11 required')
        return 1
    emit('Python version: 3.11')
    root = Path(root).resolve()
    if any(not (root/name).is_file() for name in REQUIRED_FILES):
        emit('Deployment validation: FAILED; required project files missing')
        return 1
    diagnostics = configuration_diagnostics(settings,root)
    if diagnostics:
        for diagnostic in diagnostics:
            emit(diagnostic)
        emit('Deployment validation: FAILED; configuration checks failed; values omitted')
        return 1
    for name in REQUIRED_IMPORTS:
        try:
            module_loader(name)  # Importing SDK modules does not create clients or make requests.
        except Exception:
            emit(f'Deployment validation: FAILED; dependency import unavailable: {name}')
            return 1
    emit('Configuration/structure/dependencies: PASS; secret values omitted')
    emit('No service authentication/connectivity checked; no production DB/R2 state opened or modified')
    emit('Deployment validation: PASS (offline only)')
    return 0


def main():
    if Path.cwd().resolve() != ROOT:
        print('SETTINGS_VALIDATION_FAILED: WORKING_DIRECTORY')
        print('Deployment validation: FAILED; run from repository root')
        return 1
    try:
        settings = Settings.load('.env')
    except Exception:
        print('SETTINGS_VALIDATION_FAILED: SETTINGS_LOAD')
        print('Deployment validation: FAILED; configuration error; secret values omitted')
        return 1
    try:
        return validate_deployment(settings)
    except Exception:
        print('SETTINGS_VALIDATION_FAILED: UNEXPECTED_VALIDATOR_ERROR')
        print('Deployment validation: FAILED; secret values omitted')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
