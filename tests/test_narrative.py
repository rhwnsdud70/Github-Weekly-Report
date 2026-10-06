import json
import httpx
import pytest
from services.narrative_service import OpenAIJSONClient, NarrativeService, NARRATIVE_KEYS, validate_narrative
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
    assert '근거 부족' in result['reaction'][0]
