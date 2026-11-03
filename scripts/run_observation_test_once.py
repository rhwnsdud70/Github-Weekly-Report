"""Manual isolated Observation pool/boundary/consumer runner; default offline."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None, *, environ=None, client_factory=None, http_factory=None, clock=None, emit=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('validate', 'pool', 'boundary', 'verify', 'full'), default='validate')
    parser.add_argument('--confirm-isolated-bucket', action='store_true')
    parser.add_argument('--repositories', default='', help='Up to eight public owner/repo names, comma separated')
    parser.add_argument('--cleanup-current-run', action='store_true')
    parser.add_argument('--summary', type=Path)
    args = parser.parse_args(argv)
    lines = []

    def output(line):
        lines.append(line)
        emit(line)

    try:
        if args.mode == 'validate':
            from scripts.check_observation_r2 import main as validate
            code = validate([], emit=output)
            # Compile this adapter in memory; never create a client/DB.
            for relative in ('services/observation_test_runtime.py', 'scripts/run_observation_test_once.py'):
                compile((Path(__file__).resolve().parents[1]/relative).read_bytes(), '<observation-test-runtime>', 'exec')
            output('Observation test runtime: OFFLINE ONLY; service connectivity NOT TESTED')
        else:
            from services.observation_test_runtime import run_manual, parse_names
            if not args.confirm_isolated_bucket:
                output('EXPLICIT_ISOLATED_BUCKET_CONFIRMATION_REQUIRED')
                return 1
            if environ is None:
                import os
                environ = os.environ
            kwargs = {}
            if client_factory is not None:
                kwargs['client_factory'] = client_factory
            code = run_manual(mode=args.mode, environ=environ, confirmed=True,
                names=parse_names(args.repositories), cleanup=args.cleanup_current_run,
                http_factory=http_factory, clock=clock, emit=output, **kwargs)
    except Exception as error:
        from services.observation_test_runtime import TestRuntimeError
        from services.observation_validation import ObservationValidationError
        from services.r2_observation_store import R2ObservationError
        from services.observation_r2_probe import safe_error
        category = error.category if isinstance(error, (TestRuntimeError, ObservationValidationError, R2ObservationError)) else safe_error(error).category
        output('Observation test runtime: FAILED; '+category)
        output('Production DB/Claim/Weekly Pipeline/OpenAI/Telegram: NOT ACCESSED')
        code = 1
    if args.summary:
        try:
            with args.summary.open('a', encoding='utf-8') as stream:
                stream.write('```text\n'+'\n'.join(lines)+'\n```\n')
        except OSError:
            output('SUMMARY_WRITE_FAILED')
            code = code or 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())
