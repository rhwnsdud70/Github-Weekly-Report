import json
from pathlib import Path
import time
import httpx
from services.evidence_service import (LLM_EXTRACTION_KEYS, STRUCTURED_EVIDENCE_KEYS,
    _meaningless, actor_text_supported, impact_narrative_supported, validate_extraction)

PROMPTS = Path(__file__).resolve().parents[1] / 'prompts'
NARRATIVE_KEYS = ('background','trigger','attention','reaction','project','impact',
                  'positive','concerns','debates','risks')


def object_schema(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


EXTRACTION_SCHEMA = object_schema({key:{'type':'array','items':object_schema(
    {'text':{'type':'string'}, 'source_ids':{'type':'array','items':{'type':'string'}},
     'derivation_type':{'type':'string','enum':['direct','multi_source_synthesis']},
     'grounding_terms':{'type':'array','items':{'type':'string'}}}
    if key == 'developer_impact' else
    {'text':{'type':'string'}, 'source_id':{'type':'string'}, 'quote':{'type':'string'}})}
    for key in LLM_EXTRACTION_KEYS})
NARRATIVE_SCHEMA = object_schema({key:{'type':'array','items':object_schema({
    'text':{'type':'string'}, 'source_ids':{'type':'array','items':{'type':'string'}}})}
    for key in NARRATIVE_KEYS})


class NarrativeError(RuntimeError):
    pass


def extraction_payload(evidence):
    """Only source-addressable text needed by extraction; omit bulky event/metric mirrors."""
    return {
        'repository': {'full_name':evidence['repository']['full_name'],
                       'url':evidence['repository']['url']},
        'documents': [{'source_id':document.get('id'),
                       'source_type':document.get('source_type'),
                       'title':document.get('title'),
                       'author':document.get('author'),
                       'actor_type':document.get('actor_type'),
                       'text':document.get('text'),
                       'url':document.get('url')}
                      for document in evidence['documents']],
        'deterministic_support': {key:evidence[key] for key in
            ('project_summary','attention_signals','notable_events')},
    }


class OpenAIJSONClient:
    def __init__(self, key, model, client=None, sleep=time.sleep):
        self.key, self.model = key, model
        self.client = client or httpx.Client(timeout=90)
        self.sleep = sleep
        self.last_usage = {}
        self.usages = []
        self.call_count = 0

    def generate(self, instruction, payload, schema):
        for attempt in range(3):
            try:
                self.call_count += 1
                response = self.client.post('https://api.openai.com/v1/responses',
                    headers={'Authorization':f'Bearer {self.key}'}, json={
                        'model':self.model, 'instructions':instruction,
                        'input':json.dumps(payload,ensure_ascii=False), 'store':False,
                        'max_output_tokens':6500,
                        'text':{'format':{'type':'json_schema','name':'report_content','strict':True,'schema':schema}}})
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        self.sleep(2 ** attempt)
                        continue
                if response.status_code >= 400:
                    raise NarrativeError(f'OpenAI HTTP {response.status_code}')
                data = response.json()
                if data.get('status') != 'completed':
                    raise NarrativeError('OpenAI response incomplete')
                usage = data.get('usage') or {}
                self.last_usage = {key: usage[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')
                                   if key in usage}
                self.usages.append(self.last_usage)
                text = ''.join(part['text'] for item in data.get('output', [])
                    for part in item.get('content', []) if part.get('type') == 'output_text')
                return json.loads(text)
            except (httpx.HTTPError, ValueError, KeyError):
                if attempt == 2:
                    raise NarrativeError('OpenAI response or transport failure') from None
                self.sleep(2 ** attempt)
        raise NarrativeError('OpenAI retries exhausted')


def validate_narrative(data, evidence, strict=True):
    sources = {doc['id']:doc for doc in [*evidence['documents'], *evidence.get('reference_sources', [])]}
    result = {}
    forbidden = ('대부분의 개발자', '커뮤니티 전체', '모든 개발자', '압도적인 반응', '커뮤니티는')
    for key in NARRATIVE_KEYS:
        if not isinstance(data.get(key), list):
            raise ValueError('Missing narrative section: ' + key)
        paragraphs = []
        for item in data[key]:
            text, ids = item.get('text'), item.get('source_ids')
            invalid = (not isinstance(text, str) or _meaningless(text) or not isinstance(ids, list) or not ids or
                       len(text or '') > 1400 or any(part in (text or '') for part in forbidden) or
                       any(source_id not in sources for source_id in (ids or [])))
            if invalid:
                if strict:
                    raise ValueError('Narrative unsupported, empty, or overgeneralized')
                continue
            evidence_map = {'background':'verified_facts', 'trigger':'reason_for_trending',
                'attention':'attention_signals', 'reaction':'reaction',
                'project':'project_summary', 'impact':'developer_impact',
                'positive':'positive_opinions', 'concerns':'concerns',
                'debates':'debates', 'risks':'risks'}
            if key in evidence_map:
                if key == 'impact':
                    supported = {source_id for item in evidence['developer_impact']
                                 for source_id in item.get('source_ids', [])}
                else:
                    supported = {x.get('source_id') for x in evidence[evidence_map[key]]
                                 if x.get('source_id')}
                if not set(ids) <= supported:
                    if strict:
                        raise ValueError('Narrative lacks structured evidence')
                    continue
                if key == 'impact' and not impact_narrative_supported(text, ids, evidence):
                    if strict:
                        raise ValueError('Narrative impact is not grounded')
                    continue
                if key in {'reaction','positive','concerns','debates'}:
                    actor_sources = [sources[source_id] for source_id in ids]
                    if any(not actor_text_supported(text, source.get('actor_type', 'unknown'),
                                                    source.get('author'))
                           for source in actor_sources):
                        if strict:
                            raise ValueError('Narrative actor role is unsupported')
                        continue
            links = list(dict.fromkeys(sources[source_id]['url'] for source_id in ids))
            paragraphs.append(text + '\n' + '\n'.join(links))
        result[key] = paragraphs
    project_texts = {' '.join(text.splitlines()[0].split()) for text in result.get('project', [])}
    result['background'] = [text for text in result.get('background', [])
                            if ' '.join(text.splitlines()[0].split()) not in project_texts]
    return result


def conservative_narrative(candidate):
    """Explicit fallback, also used in offline fixtures; never invent causal claims."""
    return {
        'background':[], 'trigger':[], 'attention':[], 'reaction':[],
        'project':[candidate.repository.description] if candidate.repository.description else [],
        'impact':[],
        'positive':[], 'concerns':[], 'debates':[],
        'risks':[f"{item['text']}\n{item['url']}" for item in candidate.evidence.get('risks', [])],
    }


class NarrativeService:
    def __init__(self, llm=None):
        self.llm = llm

    def generate(self, candidate):
        if self.llm is None:
            candidate.narrative = conservative_narrative(candidate)
            return candidate.narrative
        usage_start = len(getattr(self.llm, 'usages', []))
        call_start = getattr(self.llm, 'call_count', 0)
        try:
            extracted = self.llm.generate((PROMPTS/'evidence_analysis_prompt.txt').read_text(encoding='utf-8'),
                extraction_payload(candidate.evidence), EXTRACTION_SCHEMA)
            diagnostics = {}
            validated = validate_extraction(extracted, candidate.evidence,
                                            LLM_EXTRACTION_KEYS, strict=False,
                                            diagnostics=diagnostics)
            candidate.evidence['validation_diagnostics']['llm_extraction'] = diagnostics
            for key, items in validated.items():
                candidate.evidence[key].extend(items)
            narrative_payload = {
                'repository':candidate.evidence['repository'],
                'metrics':candidate.evidence['metrics'],
                'changes_since_report':candidate.evidence['changes_since_report'],
                'structured_evidence':{key:candidate.evidence[key]
                                       for key in STRUCTURED_EVIDENCE_KEYS},
            }
            generated = self.llm.generate((PROMPTS/'narrative_prompt.txt').read_text(encoding='utf-8'),
                narrative_payload, NARRATIVE_SCHEMA)
            candidate.narrative = validate_narrative(generated, candidate.evidence, strict=False)
        except (NarrativeError, ValueError, TypeError, KeyError) as error:
            candidate.errors.append(f'narrative degraded: {type(error).__name__}')
            candidate.narrative = conservative_narrative(candidate)
        usages = getattr(self.llm, 'usages', [])[usage_start:]
        candidate.evidence['llm_usage'] = {
            'calls':getattr(self.llm, 'call_count', 0)-call_start,
            'successful_calls':len(usages),
            'input_tokens':sum(item.get('input_tokens',0) for item in usages),
            'output_tokens':sum(item.get('output_tokens',0) for item in usages),
            'total_tokens':sum(item.get('total_tokens',0) for item in usages),
        }
        return candidate.narrative
