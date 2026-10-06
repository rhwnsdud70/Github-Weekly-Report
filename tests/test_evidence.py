import pytest
from models.domain import Candidate, Repository
from services.evidence_service import structure_evidence, validate_extraction


def test_evidence_provenance_and_opinion_separation():
    c = Candidate(Repository(1,'a/b','url'))
    docs = [{'id':'d1','kind':'comment','url':'issue/1#comment','text':'Easy to use','author':'person'}]
    evidence = structure_evidence(c, docs)
    assert not evidence['positive_opinions']
    extracted = validate_extraction({'positive_opinions':[{
        'source_id':'d1','quote':'Easy to use','text':'한 사용자는 사용이 쉽다고 평가했다.'}]}, evidence)
    assert extracted['positive_opinions'][0]['url'] == 'issue/1#comment'
    assert extracted['positive_opinions'][0]['scope'] == 'individual source'
    with pytest.raises(ValueError, match='Unsupported'):
        validate_extraction({'facts':[{'source_id':'invented','quote':'made up'}]}, evidence)
