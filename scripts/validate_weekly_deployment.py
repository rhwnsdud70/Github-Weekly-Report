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
    try:
        settings.validate_live(send=True)
        settings.validate_r2()
        configured_path = Path(settings.database_path)
        configured_path = configured_path if configured_path.is_absolute() else root/configured_path
        expected_path = root/'data/github_weekly_report.db'
        if configured_path.resolve() != expected_path.resolve() or settings.timezone != 'Asia/Seoul':
            raise ValueError('Invalid deployment configuration')
        if expected_path.exists() or any(Path(str(expected_path)+suffix).exists()
                                       for suffix in ('-wal','-shm','-journal')):
            raise ValueError('Local production state exists')
    except Exception:
        emit('Deployment validation: FAILED; missing/invalid settings or existing local SQLite state; values omitted')
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
        print('Deployment validation: FAILED; run from repository root')
        return 1
    try:
        return validate_deployment(Settings.load('.env'))
    except Exception:
        print('Deployment validation: FAILED; configuration error; secret values omitted')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
