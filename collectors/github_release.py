import re
from config.rules import RELEASE_SCORES
from models.domain import Event


def release_event(data):
    if data.get('draft') or data.get('prerelease') or not data.get('published_at'):
        return None
    tag = data.get('tag_name', '')
    title = data.get('name') or tag
    body = data.get('body') or ''
    kinds = ['patch']
    if re.fullmatch(r'v?1\.0(?:\.0)?', tag):
        kinds.append('stable')
    elif re.fullmatch(r'v?[2-9]\d*\.0(?:\.0)?', tag) or re.fullmatch(r'v?1\d+\.0(?:\.0)?', tag):
        kinds.append('major')
    if re.search(r'(?im)^#{1,6}\s+breaking changes?\b', body):
        kinds.append('breaking')
    # Minor/features require explicit release-note evidence, not version guesswork.
    if re.search(r'(?im)^#{1,6}\s+(new features|features added)\b', body):
        kinds.append('feature')
    kind = max(kinds,key=RELEASE_SCORES.get)
    return Event(kind, title, data['html_url'], data['published_at'], 'release',
                 {'body': body, 'tag': tag, 'classification_matches':kinds})
