import re
from config.rules import OFFICIAL_EVENT_PATTERNS
from models.domain import Event


def issue_event(item):
    kind = 'discussion'
    if item.get('author_association') in {'OWNER','MEMBER','COLLABORATOR'}:
        kind = next((kind for kind,pattern in OFFICIAL_EVENT_PATTERNS.items()
                     if re.search(pattern,item['title'],re.IGNORECASE)), kind)
    return Event(kind,item['title'],item['html_url'],item['created_at'],'issue',{
        'comments':item.get('comments'),'body':item.get('body') or '',
        'author_association':item.get('author_association'),
        'classification':'official explicit announcement' if kind != 'discussion' else 'unclassified discussion'})
