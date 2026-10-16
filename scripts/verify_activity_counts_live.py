"""Compare exact weekly GitHub Issue/PR counts through GraphQL and REST.

Run this only from a Windows PowerShell environment that can reach GitHub:
    python scripts\verify_activity_counts_live.py

This is a read-only verification utility. It neither opens the project database
nor imports/calls OpenAI or Telegram code.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from collectors.github_client import ApiBudgetExceeded, GitHubClient, GitHubError, RateLimitError
from config.settings import Settings


REPOSITORIES = (
    'mvschwarz/openrig',
    'heygen-com/hyperframes',
    'damianvtran/local-operator',
)
KST = timezone(timedelta(hours=9))
PERIOD_START = datetime(2026, 9, 27, 19, tzinfo=KST)
PERIOD_END = datetime(2026, 10, 4, 19, tzinfo=KST)


def display_count(value):
    return str(value) if value is not None else 'UNAVAILABLE'


def rest_diagnostic(github, error=None):
    """Return only safe request diagnostics; never expose request credentials."""
    summary = github.request_summary()
    rate = summary['rate_limits'].get('search', {})
    if error is None:
        status = 'SUCCESS'
        reason = None
    elif isinstance(error, ApiBudgetExceeded):
        status = 'INTERNAL_BUDGET_EXHAUSTED'
        reason = str(error)
    elif isinstance(error, RateLimitError):
        if 'reserve reached' in str(error).lower():
            status = 'RESERVE_REACHED'
        else:
            status = 'RATE_LIMIT'
        reason = str(error)
    elif isinstance(error, GitHubError):
        status = 'HTTP_ERROR' if github.last_status_code is not None else 'INVALID_RESPONSE'
        reason = str(error)
    else:
        status = 'INVALID_RESPONSE'
        reason = type(error).__name__
    return {
        'status': status,
        'http_status': github.last_status_code,
        'message': reason,
        'rate_limit': rate,
    }


def print_diagnostic(diagnostic):
    print(f"    REST status: {diagnostic['status']}")
    if diagnostic['http_status'] is not None:
        print(f"    HTTP status: {diagnostic['http_status']}")
    if diagnostic['message']:
        print(f"    REST error: {diagnostic['message']}")
    rate = diagnostic['rate_limit']
    if rate:
        for key in ('resource', 'limit', 'remaining', 'reset', 'retry_after'):
            value = 'search' if key == 'resource' else rate.get(key)
            if value is not None:
                print(f'    x-ratelimit-{key.replace("_", "-")}: {value}')


def verify_repository(github, name):
    """Return True only when both exact-count methods succeed and agree."""
    print(name)
    try:
        graphql = github.activity_counts([name], PERIOD_START, PERIOD_END).get(name, {})
    except GitHubError as error:
        graphql = {'issues': None, 'pulls': None}
        graphql_error = type(error).__name__
    else:
        graphql_error = None

    passed = True
    for kind in ('issues', 'pulls'):
        gql_count = graphql.get(kind)
        try:
            github.last_status_code = None
            rest_count = github.activity_count_fallback(name, kind, PERIOD_START, PERIOD_END)
            diagnostic = rest_diagnostic(github)
        except (GitHubError, ValueError) as error:
            rest_count = None
            diagnostic = rest_diagnostic(github, error)
        match = gql_count is not None and rest_count is not None and gql_count == rest_count
        passed = passed and match
        print(f'  {kind}:')
        print(f'    GraphQL: {display_count(gql_count)}')
        print(f'    REST: {display_count(rest_count)}')
        print(f'    match: {"YES" if match else "NO"}')
        if graphql_error:
            print(f'    GraphQL error: {graphql_error}')
        print_diagnostic(diagnostic)
    return passed


def main():
    settings = Settings.load('.env')
    if not settings.github_token:
        print('FAIL: GITHUB_TOKEN is missing or empty')
        return 2

    github = GitHubClient(settings.github_token)
    try:
        results = [verify_repository(github, name) for name in REPOSITORIES]
    finally:
        github.close()

    passed = all(results)
    print()
    print('Overall: ' + ('PASS' if passed else 'FAIL'))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
