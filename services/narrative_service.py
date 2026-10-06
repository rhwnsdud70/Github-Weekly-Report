import json
from pathlib import Path
import time
import httpx
from services.evidence_service import EVIDENCE_KEYS, validate_extraction

PROMPTS = Path(__file__).resolve().parents[1] / 'prompts'
NARRATIVE_KEYS = ('background','trigger','attention','reaction','project','impact',
                  'positive','concerns','debates','risks')


def object_schema(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


EXTRACTION_SCHEMA = object_schema({key:{'type':'array','items':object_schema({
    'text':{'type':'string'}, 'source_id':{'type':'string'}, 'quote':{'type':'string'}})}
    for key in EVIDENCE_KEYS if key != 'important_links'})
NARRATIVE_SCHEMA = object_schema({key:{'type':'array','items':object_schema({
    'text':{'type':'string'}, 'source_ids':{'type':'array','items':{'type':'string'}}})}
    for key in NARRATIVE_KEYS})


class NarrativeError(RuntimeError):
    pass


class OpenAIJSONClient:
    def __init__(self, key, model, client=None, sleep=time.sleep):
        self.key, self.model = key, model
        self.client = client or httpx.Client(timeout=90)
        self.sleep = sleep

    def generate(self, instruction, payload, schema):
        for attempt in range(3):
            try:
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
                text = ''.join(part['text'] for item in data.get('output', [])
                    for part in item.get('content', []) if part.get('type') == 'output_text')
                return json.loads(text)
            except (httpx.HTTPError, ValueError, KeyError):
                if attempt == 2:
                    raise NarrativeError('OpenAI response or transport failure') from None
                self.sleep(2 ** attempt)
        raise NarrativeError('OpenAI retries exhausted')


def validate_narrative(data, evidence):
    sources = {doc['id']:doc for doc in evidence['documents']}
    result = {}
    forbidden = ('대부분의 개발자', '커뮤니티 전체', '모든 개발자', '압도적인 반응')
    for key in NARRATIVE_KEYS:
        if not isinstance(data.get(key), list):
            raise ValueError('Missing narrative section: ' + key)
        paragraphs = []
        for item in data[key]:
            text, ids = item.get('text'), item.get('source_ids')
            if not isinstance(text, str) or not text.strip() or not isinstance(ids, list) or not ids:
                raise ValueError('Narrative requires text and evidence')
            if len(text) > 1400 or any(part in text for part in forbidden):
                raise ValueError('Narrative too long or overgeneralized')
            if any(source_id not in sources for source_id in ids):
                raise ValueError('Unknown narrative source')
            opinion_map = {'positive':'positive_opinions','concerns':'concerns','debates':'debates','risks':'risks'}
            if key in opinion_map:
                supported = {x.get('source_id') for x in evidence[opinion_map[key]]}
                if not set(ids) <= supported:
                    raise ValueError('Opinion/risk lacks extracted evidence')
            links = list(dict.fromkeys(sources[source_id]['url'] for source_id in ids))
            paragraphs.append(text + '\n' + '\n'.join(links))
        result[key] = paragraphs
    return result


def conservative_narrative(candidate):
    """Explicit fallback, also used in offline fixtures; never invent causal claims."""
    facts = candidate.evidence.get('facts', [])
    return {
        'background':[candidate.repository.description or '프로젝트 배경 근거 미확보'],
        'trigger':[f"{item['text']}\n{item['url']}" for item in facts] or ['직접 계기 확인 불가'],
        'attention':['확인된 관심 신호: ' + ', '.join(sorted(candidate.signals))],
        'reaction':['개발자가 반응한 이유에 대한 확인 가능한 근거 부족'],
        'project':[candidate.repository.description or '프로젝트 상세 설명 근거 미확보'],
        'impact':['실무 영향에 대한 확인 가능한 근거 부족'],
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
        try:
            extracted = self.llm.generate((PROMPTS/'evidence_analysis_prompt.txt').read_text(encoding='utf-8'),
                candidate.evidence, EXTRACTION_SCHEMA)
            validated = validate_extraction(extracted, candidate.evidence)
            for key, items in validated.items():
                candidate.evidence[key].extend(items)
            generated = self.llm.generate((PROMPTS/'narrative_prompt.txt').read_text(encoding='utf-8'),
                candidate.evidence, NARRATIVE_SCHEMA)
            candidate.narrative = validate_narrative(generated, candidate.evidence)
        except (NarrativeError, ValueError, TypeError, KeyError) as error:
            candidate.errors.append(f'narrative degraded: {type(error).__name__}')
            candidate.narrative = conservative_narrative(candidate)
        return candidate.narrative
