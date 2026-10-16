"""Production R2-backed one-shot entry point. Does not register/start a scheduler."""
import argparse
import logging
from pathlib import Path
import sys

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from config.settings import Settings
from services.cli import RedactSecrets, configure_logging
from services.weekly_live_runner import run_weekly_live_once


def parser():
    command = argparse.ArgumentParser(description='One production weekly run with R2 SQLite persistence')
    command.add_argument('--allow-initial-bootstrap',action='store_true',
                         help='Allow initialization only when R2 explicitly returns NoSuchKey')
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    handler = None
    try:
        settings = Settings.load('.env')
        handler = configure_logging(settings)
        handler.addFilter(RedactSecrets([settings.r2_endpoint,settings.r2_access_key_id,
                                        settings.r2_secret_access_key,settings.r2_bucket]))
        return run_weekly_live_once(settings,allow_initial_bootstrap=args.allow_initial_bootstrap).exit_code
    except Exception:
        # No raw config/path/SDK exception text or traceback on the production console.
        print('Final status: CONFIGURATION_OR_STARTUP_FAILED; exit_code=3; secrets omitted')
        return 3
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


if __name__ == '__main__':
    raise SystemExit(main())
