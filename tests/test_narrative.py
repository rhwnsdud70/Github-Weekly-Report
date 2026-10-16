import json
import httpx
import pytest
from services.narrative_service import (OpenAIJSONClient, NarrativeService, NARRATIVE_KEYS,
    extraction_payload, validate_narrative)
from services.evidence_service import structure_evidence
from models.domain import Candidate, Repository


def test_responses_api_schema():
    def respond(request):
        body = json.loads(request.content)
        assert body['text']['format']['strict'] is True
        assert body['store'] is False
        return httpx.Response(200,json={'status':'completed','output':[{'content':[{'type':'output_text','text':'{"facts":[]}'}]}]})
    client = OpenAIJSONClient('fake','test',httpx.Client(transport=httpx.MockTransport(respond)))
    assert client.generate('JSON',{}, {}) == {'facts':[]}


def test_grounding_guard_and_fallback():
    c = Candidate(Repository(1,'a/b','url',description='A developer tool'))
    structure_evidence(c,[{'id':'d1','kind':'readme','text':'tool','url':'url'}])
    data = {key:[] for key in NARRATIVE_KEYS}
    data['project'] = [{'text':'unsupported','source_ids':['invented']}]
    with pytest.raises(ValueError):
        validate_narrative(data,c.evidence)
    result = NarrativeService().generate(c)
    assert result['positive'] == []
    assert result['reaction'] == []


def test_meaningless_narrative_values_are_removed():
    c = Candidate(Repository(1,'a/b','url'))
    structure_evidence(c,[{'id':'d1','kind':'readme','text':'tool','url':'url'}])
    data = {key:[] for key in NARRATIVE_KEYS}
    data['attention'] = [{'text':'확인된 관심 신호:','source_ids':['d1']}]
    assert validate_narrative(data,c.evidence,strict=False)['attention'] == []


def test_developer_impact_narrative_preserves_all_supporting_sources():
    c = Candidate(Repository(1,'a/b','repo',description='Official project'))
    c.evidence = structure_evidence(c,[{'id':'d1','kind':'issues','source_type':'Issue',
        'text':'Workflow issue','title':'Issue','url':'issue'}])
    c.evidence['developer_impact'] = [{
        'text':'Official project 및 Workflow issue 근거는 개발 흐름에 영향을 줄 수 있다.',
        'source_ids':['d1','repository:description'], 'urls':['issue','repo'],
        'derivation_type':'multi_source_synthesis',
        'grounding_terms':['Official project','Workflow issue']}]
    data = {key:[] for key in NARRATIVE_KEYS}
    data['impact'] = [{'text':'Official project와 Workflow issue는 개발 흐름에 영향을 줄 수 있다.',
                       'source_ids':['d1','repository:description']}]
    impact = validate_narrative(data,c.evidence)['impact'][0]
    assert 'issue' in impact and 'repo' in impact


def test_extraction_payload_places_source_id_next_to_text():
    c = Candidate(Repository(1,'a/b','repo'))
    evidence = structure_evidence(c,[{'id':'d1','kind':'issues','source_type':'Issue',
        'text':'Six exact words copied from this source text.', 'title':'Issue', 'url':'issue'}])
    document = extraction_payload(evidence)['documents'][0]
    assert list(document) == ['source_id','source_type','title','author','actor_type','text','url']
    assert document['source_id'] == 'd1'


def test_narrative_preserves_bot_actor_and_does_not_promote_unknown_user():
    c = Candidate(Repository(1,'a/b','repo'))
    evidence = structure_evidence(c,[
        {'id':'d1','kind':'comment','source_type':'IssueComment','url':'bot-comment',
         'title':'Triage','author':'triage-helper[bot]','text':'Assessment: actionable.'},
        {'id':'d2','kind':'comment','source_type':'IssueComment','url':'user-comment',
         'title':'Feedback','author':'alice','text':'Setup is difficult.'},
    ])
    evidence['reaction'] = [{'text':'자동화 봇 triage-helper[bot]이 분류했다.',
        'source_id':'d1','author':'triage-helper[bot]','actor_type':'bot'}]
    evidence['concerns'] = [{'text':'GitHub 사용자 alice가 설정을 지적했다.',
        'source_id':'d2','author':'alice','actor_type':'user'}]
    data = {key:[] for key in NARRATIVE_KEYS}
    data['reaction'] = [{'text':'한 커뮤니티 구성원은 요청을 분류했다.', 'source_ids':['d1']}]
    data['concerns'] = [{'text':'프로젝트 메인테이너는 설정을 지적했다.', 'source_ids':['d2']}]
    result = validate_narrative(data,evidence,strict=False)
    assert result['reaction'] == []
    assert result['concerns'] == []
    data['reaction'] = [{'text':'자동화 봇 triage-helper[bot]은 요청을 분류했다.',
                         'source_ids':['d1']}]
    data['concerns'] = [{'text':'GitHub 사용자 alice는 설정을 지적했다.',
                         'source_ids':['d2']}]
    result = validate_narrative(data,evidence)
    assert '자동화 봇' in result['reaction'][0]
    assert 'GitHub 사용자 alice' in result['concerns'][0]


def test_rejected_impact_is_never_sent_to_narrative_generation():
    class CapturingLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, instruction, payload, schema):
            self.calls += 1
            if self.calls == 1:
                result = {key:[] for key in schema['properties']}
                result['verified_facts'] = [{'source_id':'d1',
                    'quote':'Use your existing AI subscriptions.',
                    'text':'기존 AI 구독을 사용한다.'}]
                result['developer_impact'] = [{
                    'text':'Local Operator와 AI subscriptions는 한국어 합성 기능을 제공한다.',
                    'source_ids':['d1','repository:description'],
                    'derivation_type':'multi_source_synthesis',
                    'grounding_terms':['AI subscriptions','Local Operator']}]
                return result
            assert payload['structured_evidence']['developer_impact'] == []
            result = {key:[] for key in NARRATIVE_KEYS}
            result['impact'] = [{'text':'한국어 합성 기능을 제공한다.',
                                 'source_ids':['d1','repository:description']}]
            return result

    c = Candidate(Repository(1,'a/b','repo',description='Local Operator runs on your machine'))
    structure_evidence(c,[{'id':'d1','kind':'issues','source_type':'Issue','url':'issue',
        'title':'Subscriptions','text':'Use your existing AI subscriptions.'}])
    result = NarrativeService(CapturingLLM()).generate(c)
    assert c.evidence['developer_impact'] == []
    assert result['impact'] == []
