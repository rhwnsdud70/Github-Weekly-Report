"""Source-addressable evidence: opinions stay attributed to individual documents."""
from dataclasses import asdict

EVIDENCE_KEYS = ('facts', 'release_changes', 'reason_for_trending', 'developer_impact',
    'positive_opinions', 'concerns', 'debates', 'risks', 'important_links')
OPINION_KEYS = {'positive_opinions', 'concerns', 'debates'}
SOURCE_ORDER = {'readme':0,'official_docs':0,'release':1,'issues':2,'comment':2,
                'discussions':3,'pulls':4,'security':5}


def structure_evidence(candidate, documents):
    evidence = {key: [] for key in EVIDENCE_KEYS}
    evidence['documents'] = sorted(documents, key=lambda d: SOURCE_ORDER.get(d['kind'], 9))
    evidence['metrics'] = {key: asdict(metric) for key, metric in candidate.metrics.items()}
    evidence['changes_since_report'] = candidate.changes_since_report
    evidence['repository'] = asdict(candidate.repository)
    evidence['important_links'] = [{'kind':'repository','url':candidate.repository.url}]
    for document in evidence['documents']:
        evidence['important_links'].append({'kind':document['kind'], 'url':document['url']})
    for event in candidate.events:
        evidence['facts'].append({'text':event.title, 'url':event.url, 'occurred_at':event.occurred_at,
                                  'kind':event.kind, 'type':'event'})
        if event.source == 'release':
            evidence['release_changes'].append({'text':event.title,'url':event.url,'kind':event.kind})
        if event.source == 'security':
            evidence['risks'].append({'text':event.title,'url':event.url,'details':event.details})
    candidate.evidence = evidence
    return evidence


def validate_extraction(extraction, evidence):
    sources = {document['id']:document for document in evidence['documents']}
    result = {key: [] for key in EVIDENCE_KEYS if key != 'important_links'}
    for key in result:
        items = extraction.get(key, [])
        if not isinstance(items, list):
            raise ValueError('Evidence must contain lists')
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Evidence item must be an object')
            source = sources.get(item.get('source_id'))
            quote = item.get('quote', '')
            if not source or not isinstance(quote, str) or not quote.strip() or quote not in source['text']:
                raise ValueError('Unsupported evidence quote or source')
            if key in OPINION_KEYS and source['kind'] not in {'issues','discussions','comment','pulls'}:
                raise ValueError('Community opinion must have a community source')
            result[key].append({'text':str(item.get('text', quote)), 'quote':quote,
                'source_id':source['id'], 'url':source['url'], 'author':source.get('author'),
                'scope':'individual source' if key in OPINION_KEYS else 'source claim'})
    return result
