from html.parser import HTMLParser
import httpx
from collectors.github_client import GitHubError


class TrendingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_heading = False
        self.repositories = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h2':
            self.in_heading = True
        if tag == 'a' and self.in_heading:
            name = attrs.get('href', '').strip('/')
            if len(name.split('/')) == 2 and name not in self.repositories:
                self.repositories.append(name)

    def handle_endtag(self, tag):
        if tag == 'h2':
            self.in_heading = False


def collect_trending(client):
    try:
        response = client.get('https://github.com/trending?since=weekly')
        response.raise_for_status()
    except httpx.HTTPError:
        raise GitHubError('Trending unavailable') from None
    parser = TrendingParser()
    parser.feed(response.text)
    if not parser.repositories:
        raise GitHubError('Trending markup unavailable; rank not inferred')
    return [(name, rank) for rank, name in enumerate(parser.repositories, 1)]
