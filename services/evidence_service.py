"""Source-addressable evidence: opinions stay attributed to individual documents."""
from dataclasses import asdict
import html
import re
import unicodedata
from config import rules

EVIDENCE_KEYS = ('facts', 'release_changes', 'reason_for_trending', 'developer_impact',
    'positive_opinions', 'concerns', 'debates', 'risks', 'important_links')
STRUCTURED_EVIDENCE_KEYS = ('verified_facts','project_summary','reason_for_trending',
    'attention_signals','notable_events','developer_impact','reaction',
    'positive_opinions','concerns','debates','risks')
LLM_EXTRACTION_KEYS = ('verified_facts','developer_impact','reaction',
    'positive_opinions','concerns','debates','risks')
OPINION_KEYS = {'positive_opinions', 'concerns', 'debates'}
SOURCE_ORDER = {'readme':0,'official_docs':0,'release':1,'issues':2,'comment':2,
                'discussions':3,'pulls':4,'security':5}
SOURCE_TYPES = {
    'readme':'README', 'officialdocs':'OfficialDocs', 'release':'Release',
    'issues':'Issue', 'issue':'Issue', 'issuecomment':'IssueComment',
    'discussions':'Discussion', 'discussion':'Discussion',
    'discussioncomment':'DiscussionComment', 'pulls':'PR', 'pr':'PR',
    'prcomment':'PRComment', 'security':'Security', 'comment':'Other',
}
COMMUNITY_SOURCE_TYPES = frozenset({'Issue','IssueComment','Discussion','DiscussionComment','PR','PRComment'})


def classify_actor(author, association=None, github_type=None):
    """Classify only roles GitHub metadata can support; never infer maintainer status."""
    login = str(author or '').strip()
    actor_kind = str(github_type or '').casefold()
    association = str(association or '').upper()
    if actor_kind == 'bot' or login.casefold().endswith('[bot]'):
        return 'bot'
    if association == 'OWNER':
        return 'owner'
    if association == 'CONTRIBUTOR':
        return 'contributor'
    if login:
        return 'user'
    return 'unknown'


_HUMAN_ACTOR_SUBJECT = re.compile(
    r'(?:커뮤니티 구성원|사용자|개발자|기여자|메인테이너|프로젝트 소유자)(?:은|는|이|가)|'
    r'\b(?:a|the)\s+(?:community member|user|developer|contributor|maintainer|owner)\b',
    re.IGNORECASE)
_ELEVATED_ACTOR_SUBJECT = re.compile(
    r'(?:개발자|기여자|메인테이너|프로젝트 소유자)(?:은|는|이|가)|'
    r'\b(?:a|the)\s+(?:developer|contributor|maintainer|owner)\b', re.IGNORECASE)


def actor_text_supported(text, actor_type, author=None):
    """Guard actor labels without interpreting the substance of a claim."""
    text = str(text or '')
    if actor_type == 'bot':
        bot_named = ('봇' in text.casefold() or 'bot' in text.casefold() or
                     bool(author and str(author).casefold() in text.casefold()))
        return bot_named and not _HUMAN_ACTOR_SUBJECT.search(text)
    if actor_type in {'user', 'unknown'}:
        return not _ELEVATED_ACTOR_SUBJECT.search(text)
    return True


def _readme_summary(document):
    for block in re.split(r'\n\s*\n', str(document.get('text') or '')):
        lines = [line.strip() for line in block.splitlines()
                 if line.strip() and not line.lstrip().startswith(('![', '<img', '<!--'))]
        raw = ' '.join(lines).strip()
        cleaned = re.sub(r'^#{1,6}\s*', '', raw)
        cleaned = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', cleaned)
        cleaned = re.sub(r'[`*_>#~|]+', '', cleaned).strip()
        if len(cleaned) >= 30 and not cleaned.casefold().startswith(('table of contents', 'contents ')):
            return cleaned[:600], raw
    return None, None


def _reference(evidence, source_id, title, text, url, source_type='Other'):
    evidence['reference_sources'].append({'id':source_id, 'kind':'deterministic',
        'source_type':source_type, 'title':title, 'text':str(text), 'url':url,
        'author':None, 'occurred_at':None})


def _attention(evidence, candidate, name, value, source, text):
    source_id = 'metric:' + name
    evidence['attention_signals'].append({'text':text, 'metric':name, 'value':value,
        'source':source, 'source_id':source_id})
    _reference(evidence, source_id, name, text, candidate.repository.url)


def structure_evidence(candidate, documents):
    evidence = {key: [] for key in (*EVIDENCE_KEYS, *STRUCTURED_EVIDENCE_KEYS)}
    for document in documents:
        document['source_type'] = normalize_source_type(document.get('source_type') or document.get('kind'))
        document['actor_type'] = classify_actor(
            document.get('author'), document.get('author_association'), document.get('github_actor_type'))
    evidence['documents'] = sorted(documents, key=lambda d: SOURCE_ORDER.get(d['kind'], 9))
    evidence['metrics'] = {key: asdict(metric) for key, metric in candidate.metrics.items()}
    evidence['changes_since_report'] = candidate.changes_since_report
    evidence['repository'] = asdict(candidate.repository)
    if candidate.observation.get('enabled'):
        selected = ((candidate.observation.get('repository') or {}).get('current') or {}).get('snapshot') or {}
        evidence['repository_observed_at'] = selected.get('provenance',{}).get('observed_at')
        evidence['latest_repository'] = asdict(candidate.latest_repository)
        evidence['latest_repository_observed_at'] = candidate.latest_observed_at
        evidence['repository'].update({
            'star_fork_basis':'boundary evaluation observation',
            'star_fork_observed_at':evidence['repository_observed_at'],
            'latest_stars':candidate.latest_repository.stars,
            'latest_forks':candidate.latest_repository.forks,
            'latest_observed_at':candidate.latest_observed_at,
        })
    evidence['source_manifest'] = getattr(candidate, 'deep_analysis_manifest', {})
    evidence['validation_diagnostics'] = {
        'collection': getattr(candidate, 'deep_analysis_diagnostics', {})}
    evidence['reference_sources'] = []
    evidence['important_links'] = [{'kind':'repository','url':candidate.repository.url}]
    for document in evidence['documents']:
        evidence['important_links'].append({'kind':document['kind'], 'url':document['url']})

    description = str(candidate.repository.description or '').strip()
    if description:
        source_id = 'repository:description'
        _reference(evidence, source_id, 'GitHub repository description', description,
                   candidate.repository.url, 'OfficialDocs')
        evidence['project_summary'].append({'text':description, 'quote':description,
            'source_id':source_id, 'url':candidate.repository.url, 'author':None,
            'source_type':'OfficialDocs', 'title':'GitHub repository description',
            'occurred_at':None, 'scope':'source claim', 'deterministic':True})
    else:
        readme = next((item for item in evidence['documents'] if item['source_type']=='README'), None)
        if readme:
            text, quote = _readme_summary(readme)
            if text:
                evidence['project_summary'].append({'text':text, 'quote':quote,
                    'source_id':readme['id'], 'url':readme['url'], 'author':readme.get('author'),
                    'source_type':'README', 'title':readme.get('title'),
                    'occurred_at':readme.get('occurred_at'), 'scope':'source claim',
                    'deterministic':True})

    for event in candidate.events:
        document = next((item for item in evidence['documents'] if item['url'] == event.url), None)
        source_id = document['id'] if document else None
        evidence['facts'].append({'text':event.title, 'url':event.url, 'occurred_at':event.occurred_at,
                                  'kind':event.kind, 'type':'event'})
        if event.source == 'release':
            evidence['release_changes'].append({'text':event.title,'url':event.url,'kind':event.kind})
        if event.source == 'security':
            risk = {'text':event.title,'url':event.url,'details':event.details,
                    'source_type':'Security'}
            if source_id:
                risk['source_id'] = source_id
            evidence['risks'].append(risk)
        direct_release = (event.source == 'release' and
                          rules.RELEASE_SCORES.get(event.kind, 0) >= rules.RELEASE_SCORES['major'])
        if source_id and (event.source == 'security' or direct_release):
            direct = {'text':event.title,'url':event.url,
                'occurred_at':event.occurred_at,'event_kind':event.kind,
                'source_type':normalize_source_type(event.source),'direct_signal':True}
            if source_id:
                direct['source_id'] = source_id
            evidence['reason_for_trending'].append(direct)
    notable_priority = {'Security':0, 'Release':1, 'Issue':2, 'PR':3}
    notable_documents = sorted((item for item in evidence['documents']
        if item['source_type'] in notable_priority),
        key=lambda item:notable_priority[item['source_type']])[:rules.DEEP_NOTABLE_LIMIT]
    for document in notable_documents:
        evidence['notable_events'].append({'text':document.get('title') or document['text'][:160],
            'url':document['url'], 'occurred_at':document.get('occurred_at'),
            'source_type':document['source_type'], 'source_id':document['id'],
            'deterministic':True})

    metric_labels = {
        'trending_rank':'Trending rank', 'star_delta':'7-day star delta',
        'star_growth':'7-day star growth %', 'issues':'Weekly issue activity',
        'pulls':'Weekly PR activity', 'issues_growth':'Issue WoW growth %',
        'pulls_growth':'PR WoW growth %', 'activity':'Weekly Issue/PR activity',
        'activity_growth':'Issue/PR WoW growth %',
    }
    for name, metric in evidence['metrics'].items():
        if metric['status'] != 'confirmed':
            continue
        if name in metric_labels:
            _attention(evidence, candidate, name, metric['value'], metric['source'],
                       f'{metric_labels[name]}: {metric["value"]}')
    releases = [event for event in candidate.events if event.source == 'release']
    if releases:
        _attention(evidence, candidate, 'release_activity', len(releases), 'GitHub releases',
                   f'Releases published in report period: {len(releases)}')
        major_releases = [event for event in releases
                          if rules.RELEASE_SCORES.get(event.kind, 0) >= rules.RELEASE_SCORES['major']]
        if major_releases:
            _attention(evidence, candidate, 'major_releases', len(major_releases), 'GitHub releases',
                       f'Major or breaking releases in report period: {len(major_releases)}')
    security_events = [event for event in candidate.events if event.source == 'security']
    if security_events:
        _attention(evidence, candidate, 'security_events', len(security_events), 'GitHub advisories',
                   f'Security events in report period: {len(security_events)}')
    candidate.evidence = evidence
    return evidence


def _meaningless(text):
    normalized = ' '.join(str(text or '').split()).rstrip(':')
    return not normalized or normalized in {'확인된 관심 신호', '확인 불가', '근거 부족',
        '직접 계기 확인 불가', '개발자가 반응한 이유에 대한 확인 가능한 근거 부족',
        '실무 영향에 대한 확인 가능한 근거 부족'}


_IMPACT_DERIVATION_TYPES = frozenset({'direct', 'multi_source_synthesis'})
_IMPACT_CAPABILITY_WORDS = re.compile(
    r'(?:기능|지원|합성|통합|연동|생성|실행|처리|호환|자동화)')
_IMPACT_LATIN_WORDS = re.compile(r"[A-Za-z][A-Za-z0-9+#._/-]{2,}")
_IMPACT_COMMON_LATIN = frozenset({
    'and','are','can','developer','developers','development','from','into','may',
    'project','release','releases','the','their','this','through','tool','tools',
    'use','uses','using','with','workflow','workflows',
})


def normalize_source_type(value):
    compact = re.sub(r'[^a-z]', '', str(value or '').lower())
    return SOURCE_TYPES.get(compact, 'Other')


def _normalized_text(value):
    text = unicodedata.normalize('NFKC', html.unescape(str(value or '')))
    text = text.translate(str.maketrans({'“':'"','”':'"','‘':"'",'’':"'",' ':' '}))
    text = re.sub(r'```[^\n]*\n?', ' ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'!\[([^]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\\([`*_{}\[\]()#+.!>~-])', r'\1', text)
    text = re.sub(r'(?m)^\s*(?:[-+*]|\d+[.)])\s+', '', text)
    text = re.sub(r'[`*_>#~|]+', ' ', text)
    return ' '.join(text.split()).strip(' .…').casefold()


def _impact_grounding_reason(text, source_ids, grounding_terms, all_sources, derivation_type):
    """Reject impact prose that cannot be traced to exact supporting source terms."""
    if derivation_type not in _IMPACT_DERIVATION_TYPES:
        return 'malformed'
    if (not isinstance(grounding_terms, list) or not grounding_terms or
            any(not isinstance(term, str) for term in grounding_terms)):
        return 'impact_not_grounded'
    if len(text) < 20:
        return 'impact_not_grounded'
    normalized_impact = _normalized_text(text)
    source_texts = {source_id:_normalized_text(all_sources[source_id].get('text'))
                    for source_id in source_ids}
    grounded_source_ids = set()
    for term in grounding_terms:
        normalized_term = _normalized_text(term)
        if not normalized_term or len(normalized_term) < 2 or normalized_term not in normalized_impact:
            return 'impact_not_grounded'
        matched = {source_id for source_id, source_text in source_texts.items()
                   if normalized_term in source_text}
        if not matched:
            return 'impact_not_grounded'
        grounded_source_ids.update(matched)
    required_sources = 2 if derivation_type == 'multi_source_synthesis' else 1
    if len(grounded_source_ids) < required_sources:
        return 'impact_not_grounded'

    combined_sources = ' '.join(source_texts.values())
    for token in _IMPACT_LATIN_WORDS.findall(text):
        normalized_token = _normalized_text(token)
        if normalized_token not in _IMPACT_COMMON_LATIN and normalized_token not in combined_sources:
            return 'impact_not_grounded'
    # Capability claims written only in Korean cannot be verified against English-only sources.
    # Keep names/capabilities verbatim as grounding terms and use Korean only for the work impact.
    for match in _IMPACT_CAPABILITY_WORDS.finditer(text):
        start = max(0, match.start() - 24)
        phrase = _normalized_text(text[start:match.end()])
        if re.search(r'[가-힣]', phrase) and match.group() not in combined_sources:
            return 'impact_not_grounded'
    return None


def impact_narrative_supported(text, source_ids, evidence):
    """A narrative impact may only restate an already validated impact item."""
    all_sources = {source['id']:source for source in
                   [*evidence.get('documents', []), *evidence.get('reference_sources', [])]}
    requested = set(source_ids or [])
    for impact in evidence.get('developer_impact', []):
        impact_ids = impact.get('source_ids', [])
        if requested and requested <= set(impact_ids):
            if _impact_grounding_reason(text, impact_ids, impact.get('grounding_terms'),
                                        all_sources, impact.get('derivation_type')) is None:
                return True
    return False


def _reject(diagnostics, reason):
    if diagnostics is None:
        return
    diagnostics['rejected'] = diagnostics.get('rejected', 0) + 1
    reasons = diagnostics.setdefault('reasons', {})
    reasons[reason] = reasons.get(reason, 0) + 1


def _rejection_item(source_id, source, quote, reason):
    source_text = str((source or {}).get('text') or '')
    quote_text = str(quote or '')
    return {'source_id':source_id, 'source_type':(source or {}).get('source_type','unknown'),
        'quote_length':len(quote_text), 'raw_match':bool(quote_text and quote_text in source_text),
        'normalized_match':bool(_normalized_text(quote_text) and
            _normalized_text(quote_text) in _normalized_text(source_text)),
        'reason':reason}


def validate_extraction(extraction, evidence, keys=None, strict=True, diagnostics=None):
    sources = {document['id']:document for document in evidence['documents']}
    all_sources = {source['id']:source for source in
                   [*evidence['documents'], *evidence.get('reference_sources', [])]}
    direct_trend_sources = {item.get('source_id') for item in evidence.get('reason_for_trending', [])
                            if item.get('direct_signal') and item.get('source_id')}
    keys = keys or tuple(key for key in EVIDENCE_KEYS if key != 'important_links')
    result = {key: [] for key in keys}
    community_keys = OPINION_KEYS | {'reaction'}
    seen = set()
    if diagnostics is not None:
        diagnostics.update({'accepted':0,'rejected':0,'reasons':{},'items':[]})
    for key in result:
        items = extraction.get(key, [])
        if not isinstance(items, list):
            raise ValueError('Evidence must contain lists')
        for item in items:
            if not isinstance(item, dict):
                _reject(diagnostics, 'malformed')
                if strict: raise ValueError('Evidence item must be an object')
                continue
            if key == 'developer_impact':
                text = str(item.get('text') or '').strip() if isinstance(item,dict) else ''
                source_ids = item.get('source_ids') if isinstance(item,dict) else None
                derivation_type = item.get('derivation_type') if isinstance(item,dict) else None
                grounding_terms = item.get('grounding_terms') if isinstance(item,dict) else None
                project_ids = {value.get('source_id') for value in evidence.get('project_summary', [])}
                attention_ids = {value.get('source_id') for value in evidence.get('attention_signals', [])}
                notable_ids = {value.get('source_id') for value in evidence.get('notable_events', [])}
                verified_ids = {value.get('source_id') for value in result.get('verified_facts', [])}
                categories = set()
                for source_id in source_ids or []:
                    if source_id in verified_ids: categories.add('verified_facts')
                    if source_id in project_ids: categories.add('project_summary')
                    if source_id in attention_ids: categories.add('attention_signals')
                    if source_id in notable_ids: categories.add('notable_events')
                reason = None
                if not isinstance(item,dict) or not isinstance(source_ids,list): reason = 'malformed'
                elif _meaningless(text): reason = 'empty_text'
                elif not source_ids or any(source_id not in all_sources for source_id in source_ids):
                    reason = 'source_id_not_found'
                elif derivation_type == 'multi_source_synthesis' and len(set(source_ids)) < 2:
                    reason = 'insufficient_synthesis_sources'
                elif derivation_type == 'multi_source_synthesis' and len(categories) < 2:
                    reason = 'insufficient_synthesis_sources'
                elif derivation_type == 'direct' and not categories:
                    reason = 'insufficient_synthesis_sources'
                else:
                    reason = _impact_grounding_reason(
                        text, list(dict.fromkeys(source_ids)), grounding_terms,
                        all_sources, derivation_type)
                if reason:
                    _reject(diagnostics, reason)
                    if diagnostics is not None:
                        diagnostics['items'].append({'source_id':','.join(map(str,source_ids or [])),
                            'source_type':'multi-source', 'quote_length':0,
                            'raw_match':False, 'normalized_match':False, 'reason':reason})
                    if strict: raise ValueError('Unsupported developer impact synthesis')
                    continue
                unique_ids = list(dict.fromkeys(source_ids))
                result[key].append({'text':text, 'source_ids':unique_ids,
                    'urls':list(dict.fromkeys(all_sources[source_id]['url'] for source_id in unique_ids)),
                    'source_types':list(dict.fromkeys(all_sources[source_id].get('source_type','Other')
                                                     for source_id in unique_ids)),
                    'derivation_type':derivation_type,
                    'grounding_terms':list(dict.fromkeys(grounding_terms)),
                    'scope':'multi-source synthesis' if derivation_type == 'multi_source_synthesis'
                            else 'direct source impact'})
                if diagnostics is not None: diagnostics['accepted'] += 1
                continue
            source = sources.get(item.get('source_id'))
            quote = item.get('quote', '')
            text = str(item.get('text', quote)).strip()
            reason = None
            if not source: reason = 'source_id_not_found'
            elif _meaningless(text): reason = 'empty_text'
            elif not _normalized_text(quote) or _normalized_text(quote) not in _normalized_text(source['text']):
                reason = 'quote_not_found'
            elif key in community_keys and source['source_type'] not in COMMUNITY_SOURCE_TYPES:
                reason = 'unsupported_source_type'
            elif key in community_keys and not actor_text_supported(
                    text, source.get('actor_type', 'unknown'), source.get('author')):
                reason = 'actor_mismatch'
            elif key == 'project_summary' and source['source_type'] not in {'README','OfficialDocs'}:
                reason = 'unsupported_source_type'
            elif key == 'reason_for_trending' and item.get('source_id') not in direct_trend_sources:
                reason = 'unsupported_source_type'
            identity = (key, item.get('source_id'), _normalized_text(text))
            if not reason and identity in seen: reason = 'duplicate'
            if reason:
                _reject(diagnostics, reason)
                if diagnostics is not None:
                    diagnostics['items'].append(_rejection_item(item.get('source_id'),source,quote,reason))
                if strict: raise ValueError('Unsupported evidence quote or source')
                continue
            seen.add(identity)
            result[key].append({'text':text, 'quote':quote,
                'source_id':source['id'], 'url':source['url'], 'author':source.get('author'),
                'actor_type':source.get('actor_type', 'unknown'),
                'source_type':source.get('source_type', source['kind']),
                'title':source.get('title'), 'occurred_at':source.get('occurred_at'),
                'scope':'individual source' if key in community_keys else 'source claim'})
            if diagnostics is not None:
                diagnostics['accepted'] += 1
    return result
