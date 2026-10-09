import re
from urllib.parse import urlparse
from models.domain import Event


def advisory_repository(data):
    url = data.get('source_code_location') or ''
    parsed = urlparse(url)
    if parsed.netloc == 'github.com' and len(parsed.path.strip('/').split('/')) >= 2:
        return '/'.join(parsed.path.strip('/').split('/')[:2])
    api = data.get('repository_advisory_url') or ''
    if api.startswith('https://api.github.com/repos/'):
        return '/'.join(urlparse(api).path.split('/')[2:4])
    return None


def security_event(data):
    if data.get('withdrawn_at') or data.get('severity') not in ('critical', 'high'):
        return None
    kind = data['severity']
    if re.search(r'\b(?:remote code execution|RCE)\b',data['summary'],re.IGNORECASE):
        kind = 'rce'
    elif re.search(r'\bsupply[- ]chain attack\b',data['summary'],re.IGNORECASE):
        kind = 'supply_chain'
    vector = (data.get('cvss') or {}).get('vector_string') or ''
    easy = all(part in vector.split('/') for part in ('AV:N','AC:L','PR:N','UI:N'))
    return Event(kind, data['summary'], data['html_url'], data['published_at'], 'security',
        {'description': data.get('description', ''), 'vulnerabilities': data.get('vulnerabilities', []),
         'cvss': data.get('cvss', {}), 'severity': data['severity'], 'easy_to_exploit':easy})
