"""Run only small, live GitHub API connectivity checks.

This script never invokes the reporting pipeline, OpenAI, or Telegram. It does
not print secrets, request headers, response bodies, usernames, or repository
content. Run it from the repository root:

    python scripts/test_github_live.py
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

# Executing ``python scripts/test_github_live.py`` sets sys.path to scripts/.
# Add only this repository root so the existing project modules are reused.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.github_client import GitHubClient, GitHubError, RateLimitError
from config.settings import Settings


REPOSITORY = 'python/cpython'


def result(name, success, status=None, error_type=None, reason=None):
    return {
        'name': name,
        'success': success,
        'status': status,
        'error_type': error_type,
        'reason': reason,
    }


def execute(client, name, operation):
    client.last_status_code = None
    client.last_error_details = None
    try:
        operation()
        return result(name, True, client.last_status_code)
    except RateLimitError:
        return result(name, False, client.last_status_code, 'RateLimitError',
                      'GitHub rate limit reached')
    except GitHubError as error:
        item = result(name, False, client.last_status_code, type(error).__name__,
                      str(error))
        item['response_details'] = client.last_error_details if client.last_status_code == 422 else None
        return item
    except Exception as error:
        # Do not expose library exception messages because a proxy could include
        # request details. The exception type is enough for connectivity triage.
        return result(name, False, client.last_status_code, type(error).__name__,
                      'Unexpected transport or configuration failure')


def checks(settings, client):
    """Return all checks; no failure prevents a later check from running."""
    outcomes = [result('.env GITHUB_TOKEN loading', bool(settings.github_token), None,
                       None if settings.github_token else 'ConfigurationError',
                       None if settings.github_token else 'GITHUB_TOKEN is missing or empty')]
    outcomes.append(execute(client, 'GitHub Rate Limit', lambda: client.request('rate_limit')))
    outcomes.append(execute(client, 'Public Repository', lambda: client.repository(REPOSITORY)))
    outcomes.append(execute(client, 'Issues', lambda: client.issues(REPOSITORY, 'is:open', limit=1)))
    outcomes.append(execute(client, 'Pull Requests', lambda: client.pulls(REPOSITORY, 'is:open', limit=1)))
    outcomes.append(execute(client, 'Releases', lambda: client.releases(REPOSITORY)))
    outcomes.append(execute(client, 'Discussions GraphQL', lambda: client.discussions(REPOSITORY, limit=1)))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    date_range = f'{start.date().isoformat()}..{end.date().isoformat()}'
    timestamp_range = f'{start.isoformat()}..{end.isoformat()}'
    # Baseline first: this is the same request that succeeds with curl.exe.
    outcomes.append(execute(client, 'Global Security Advisories: GET /advisories',
                            lambda: client.request('advisories')))
    # This reproduces the old collection parameter exactly enough to reveal a
    # validation failure, while later probes isolate every documented option.
    outcomes.append(execute(client, 'Advisories parameter: published timestamp range',
                            lambda: client.request('advisories', {'published': timestamp_range})))
    for label, params in (
        ('published date range', {'published': date_range}),
        ('updated date range', {'updated': date_range}),
        ('modified date range', {'modified': date_range}),
        ('severity', {'severity': 'critical'}),
        ('type', {'type': 'reviewed'}),
        ('ecosystem', {'ecosystem': 'pip'}),
        ('sort', {'sort': 'published'}),
        ('direction', {'direction': 'desc'}),
        ('per_page', {'per_page': 1}),
    ):
        outcomes.append(execute(client, f'Advisories parameter: {label}',
                                lambda params=params: client.request('advisories', params)))
    return outcomes


def print_result(item):
    print(f"항목명: {item['name']}")
    print('결과: ' + ('성공' if item['success'] else '실패'))
    print('HTTP Status: ' + (str(item['status']) if item['status'] is not None else 'N/A'))
    if not item['success']:
        print(f"오류: {item['error_type']} - {item['reason']}")
        if item.get('response_details'):
            for key in ('message', 'errors', 'documentation_url'):
                if key in item['response_details']:
                    print(f'{key}: {item["response_details"][key]}')


def main():
    try:
        settings = Settings.load('.env')
    except Exception as error:
        # Remaining API calls cannot be authenticated without settings, but are
        # intentionally not attempted in this configuration-failure case.
        for name in ('.env GITHUB_TOKEN loading', 'GitHub Rate Limit', 'Public Repository',
                     'Issues', 'Pull Requests', 'Releases', 'Discussions GraphQL',
                     'Global Security Advisories'):
            print_result(result(name, False, None, type(error).__name__,
                                'Configuration loading failed'))
        return 1
    # A diagnostic should continue quickly when a local firewall blocks every
    # request. Production collection keeps the normal retry backoff.
    client = GitHubClient(settings.github_token, sleep=lambda _: None)
    try:
        outcomes = checks(settings, client)
    finally:
        client.close()
    for item in outcomes:
        print_result(item)
    # The legacy timestamp probe intentionally records whether the historical
    # parameter is rejected. Its failure is diagnostic, not a script failure.
    required = [item for item in outcomes if item['name'] !=
                'Advisories parameter: published timestamp range']
    return 0 if all(item['success'] for item in required) else 1


if __name__ == '__main__':
    raise SystemExit(main())
