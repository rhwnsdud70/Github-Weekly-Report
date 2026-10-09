"""Command entry point; orchestration and domain logic live in services."""
from services.cli import main


if __name__ == '__main__':
    raise SystemExit(main())
