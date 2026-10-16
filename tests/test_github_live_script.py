from config.settings import Settings
from collectors.github_client import GitHubError, RateLimitError
from scripts.test_github_live import checks, print_result


class FakeClient:
    def __init__(self):
        self.last_status_code = None
        self.calls = []

    def request(self, path, params=None, body=None):
        self.calls.append((path, params))
        self.last_status_code = 200
        return {}

    def repository(self, name):
        self.calls.append(('repository', name))
        self.last_status_code = 200
        return {}

    def issues(self, name, query, limit):
        self.calls.append(('issues', name))
        self.last_status_code = 200
        raise GitHubError('GitHub HTTP 403')

    def pulls(self, name, query, limit):
        self.calls.append(('pulls', name))
        self.last_status_code = 200
        return []

    def releases(self, name):
        self.calls.append(('releases', name))
        self.last_status_code = 200
        return []

    def discussions(self, name, limit):
        self.calls.append(('discussions', name))
        self.last_status_code = 200
        raise RateLimitError('ignored')



def test_checks_continue_and_do_not_render_a_secret(capsys):
    client = FakeClient()
    outcomes = checks(Settings(github_token='secret-value'), client)
    assert len(outcomes) == 18
    assert outcomes[3]['success'] is False
    assert outcomes[6]['error_type'] == 'RateLimitError'
    assert client.calls[-1] == ('advisories', {'per_page': 1})
    for outcome in outcomes:
        print_result(outcome)
    output = capsys.readouterr().out
    assert 'secret-value' not in output
    assert 'HTTP Status: 200' in output
