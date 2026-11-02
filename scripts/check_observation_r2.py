"""Phase 7B only. Default is offline; live requires explicit isolated-bucket consent."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.observation_r2_probe import run_probe, validate_sdk_contract


def main(argv=None, *, environ=None, client_factory=None, emit=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('validate', 'live'), default='validate')
    parser.add_argument('--confirm-isolated-bucket', action='store_true')
    parser.add_argument('--run-id', default='0')
    parser.add_argument('--attempt', default='1')
    parser.add_argument('--summary', type=Path)
    args = parser.parse_args(argv)
    if args.mode == 'validate':
        try:
            validate_sdk_contract()
            from scripts.validate_observation_deployment import validate_observation_deployment
            passed = validate_observation_deployment(emit=emit)
            # Existing validator returns a process exit code, not a boolean.
            code = passed
            lines = ('Observation R2 Probe: OFFLINE VALIDATION ' + ('SUCCESS' if code == 0 else 'FAILED'),
                     'Service connectivity/authentication: NOT TESTED', 'Production: NOT EXECUTED')
        except Exception:
            code = 1
            lines = ('Observation R2 Probe: OFFLINE VALIDATION FAILED; SDK_OR_INTERFACE_CONTRACT',
                     'Service connectivity/authentication: NOT TESTED', 'Production: NOT EXECUTED')
    else:
        if environ is None:
            import os
            environ = os.environ  # No dotenv, Settings, or production-variable fallback.
        kwargs = {'client_factory': client_factory} if client_factory is not None else {}
        result = run_probe(environ=environ, run_id=args.run_id, attempt=args.attempt,
                           confirmed=args.confirm_isolated_bucket, **kwargs)
        code, lines = result.exit_code, result.lines()
    for line in lines:
        emit(line)
    if args.summary:
        try:
            with args.summary.open('a', encoding='utf-8') as stream:
                stream.write('```text\n' + '\n'.join(lines) + '\n```\n')
        except OSError:
            emit('SUMMARY_WRITE_FAILED')
            return code or 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())
