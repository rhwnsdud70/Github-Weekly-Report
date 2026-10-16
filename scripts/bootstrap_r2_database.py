"""Initial local production SQLite migration to R2; no pipeline or scheduler."""
import argparse
from pathlib import Path
import sys

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from config.settings import Settings
from database.sqlite_backup import SQLiteBackupError
from services.r2_persistence import R2DatabaseStore, R2PersistenceError


def main(argv=None):
    argparse.ArgumentParser(description='Create the first R2 production DB; never overwrite').parse_args(argv)
    store = None
    try:
        settings = Settings.load('.env')
        settings.validate_r2()
        store = R2DatabaseStore(settings)
        print('Mode: initial-r2-migration (SQLite writers must be stopped)')
        result = store.bootstrap()
        print('SQLite schema/integrity/WAL export: PASS')
        print(f'R2 object: {result["key"]}; exists=YES; bytes={result["size"]}')
        print(f'SHA256 verified: {result["sha256"]}')
        print('Initial R2 migration: SUCCESS')
        return 0
    except R2PersistenceError as error:
        print(f'Initial R2 migration: FAILED; {error}')
        print('Remote state is not deleted or overwritten. Verify it before any further action.')
        return 1
    except SQLiteBackupError:
        print('Initial R2 migration: FAILED; SQLITE_VALIDATION_OR_CHECKPOINT_FAILED')
        return 1
    except Exception:
        print('Initial R2 migration: FAILED; CONFIGURATION_OR_LOCAL_IO_ERROR; values omitted')
        return 1
    finally:
        if store is not None:
            if store.bootstrap_backup_path is not None:
                # Do not echo configured paths: they may contain sensitive values.
                print('Local backup retained in the DB directory: r2-bootstrap-backups/ (validate before recovery)')
            try:
                store.close()
            except Exception:
                print('Initial R2 migration: FAILED; CLIENT_CLOSE_ERROR; remote state requires verification')
                return 1


if __name__ == '__main__':
    raise SystemExit(main())
