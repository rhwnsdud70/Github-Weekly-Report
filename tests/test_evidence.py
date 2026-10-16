import pytest
from models.domain import Candidate, Event, Metric, Repository
from services.evidence_service import structure_evidence, validate_extraction


def test_evidence_provenance_and_opinion_separation():
    c = Candidate(Repository(1,'a/b','url'))
    docs = [{'id':'d1','kind':'comment','source_type':'IssueComment',
             'url':'issue/1#comment','text':'Easy to use','author':'person'}]
    evidence = structure_evidence(c, docs)
    assert not evidence['positive_opinions']
    extracted = validate_extraction({'positive_opinions':[{
        'source_id':'d1','quote':'Easy to use','text':'한 사용자는 사용이 쉽다고 평가했다.'}]}, evidence)
    assert extracted['positive_opinions'][0]['url'] == 'issue/1#comment'
    assert extracted['positive_opinions'][0]['scope'] == 'individual source'
    with pytest.raises(ValueError, match='Unsupported'):
        validate_extraction({'facts':[{'source_id':'invented','quote':'made up'}]}, evidence)


def test_readme_project_summary_survives_markdown_and_newline_normalization():
    candidate = Candidate(Repository(1, 'a/b', 'https://github.com/a/b'))
    evidence = structure_evidence(candidate, [{'id':'d1','kind':'readme','source_type':'README',
        'url':'readme','title':'README','text':'## Local Operator\nBuild **local AI agents** safely.'}])
    assert evidence['project_summary'][0]['source_id'] == 'd1'
    assert evidence['project_summary'][0]['deterministic'] is True
    diagnostics = {}
    result = validate_extraction({'project_summary':[{'source_id':'d1',
        'quote':'Local Operator Build local AI agents safely.',
        'text':'로컬 AI 에이전트를 만드는 프로젝트다.'}]}, evidence,
        keys=('project_summary',), diagnostics=diagnostics)
    assert result['project_summary'][0]['source_type'] == 'README'
    assert diagnostics == {'accepted':1, 'rejected':0, 'reasons':{}, 'items':[]}


def test_quote_normalization_accepts_markdown_newlines_html_and_smart_quotes():
    candidate = Candidate(Repository(1, 'a/b', 'url'))
    evidence = structure_evidence(candidate, [{'id':'d1','kind':'comment',
        'source_type':'IssueComment','url':'comment','title':'Feedback',
        'text':'```text\nThe **“local cache”** is useful &amp; fast.\n```'}])
    result = validate_extraction({'reaction':[{'source_id':'d1',
        'quote':'The "local cache" is useful & fast.', 'text':'한 사용자는 로컬 캐시가 유용하다고 평가했다.'}]},
        evidence, keys=('reaction',))
    assert len(result['reaction']) == 1


@pytest.mark.parametrize('source_type', ['IssueComment', 'PRComment'])
def test_comment_source_types_are_valid_community_evidence(source_type):
    candidate = Candidate(Repository(1, 'a/b', 'url'))
    evidence = structure_evidence(candidate, [{'id':'d1','kind':'comment',
        'source_type':source_type,'url':'comment','title':'Feedback','author':'person',
        'text':'The cache is useful, but setup is difficult.'}])
    result = validate_extraction({'reaction':[{'source_id':'d1',
        'quote':'The cache is useful, but setup is difficult.',
        'text':'한 사용자는 캐시를 유용하다고 평가하면서 설정 난도를 언급했다.'}]},
        evidence, keys=('reaction',))
    assert result['reaction'][0]['source_type'] == source_type
    assert result['reaction'][0]['author'] == 'person'


def test_invalid_quote_is_rejected_with_safe_diagnostic():
    candidate = Candidate(Repository(1, 'a/b', 'url'))
    evidence = structure_evidence(candidate, [{'id':'d1','kind':'issues','source_type':'Issue',
        'url':'issue','title':'Issue','text':'Actual text'}])
    diagnostics = {}
    result = validate_extraction({'reaction':[{'source_id':'d1','quote':'Invented quote',
        'text':'일부 사용자가 문제를 제기했다.'}]}, evidence, keys=('reaction',),
        strict=False, diagnostics=diagnostics)
    assert result['reaction'] == []
    assert diagnostics['accepted'] == 0
    assert diagnostics['rejected'] == 1
    assert diagnostics['reasons'] == {'quote_not_found':1}
    assert diagnostics['items'] == [{'source_id':'d1','source_type':'Issue',
        'quote_length':14,'raw_match':False,'normalized_match':False,
        'reason':'quote_not_found'}]


def test_short_verbatim_quote_passes_but_paraphrase_stays_rejected():
    candidate = Candidate(Repository(1,'a/b','url'))
    evidence = structure_evidence(candidate,[{'id':'d1','kind':'issues','source_type':'Issue',
        'url':'issue','title':'Issue','author':'alice',
        'text':'Agents cannot revise an answered question before final delivery.'}])
    accepted = validate_extraction({'concerns':[{'source_id':'d1',
        'quote':'cannot revise an answered question before final delivery',
        'text':'GitHub 사용자 alice는 답변 수정 제한을 지적했다.'}]}, evidence,
        keys=('concerns',), strict=False, diagnostics={})
    assert len(accepted['concerns']) == 1
    rejected = validate_extraction({'concerns':[{'source_id':'d1',
        'quote':'cannot change a completed answer before delivery',
        'text':'GitHub 사용자 alice는 답변 수정 제한을 지적했다.'}]}, evidence,
        keys=('concerns',), strict=False, diagnostics={})
    assert rejected['concerns'] == []


def test_actor_type_blocks_bot_as_human_and_unknown_role_promotion():
    candidate = Candidate(Repository(1,'a/b','url'))
    evidence = structure_evidence(candidate,[
        {'id':'d1','kind':'comment','source_type':'IssueComment','url':'bot-comment',
         'title':'Triage','author':'triage-helper[bot]','text':'Assessment: actionable feature request.'},
        {'id':'d2','kind':'comment','source_type':'IssueComment','url':'user-comment',
         'title':'Feedback','author':'alice','text':'The setup is difficult.'},
    ])
    assert evidence['documents'][0]['actor_type'] == 'bot'
    invalid = validate_extraction({'reaction':[{'source_id':'d1',
        'quote':'Assessment: actionable feature request.',
        'text':'한 커뮤니티 구성원은 요청을 처리 가능하다고 평가했다.'}]}, evidence,
        keys=('reaction',), strict=False, diagnostics={})
    assert invalid['reaction'] == []
    valid = validate_extraction({'reaction':[{'source_id':'d1',
        'quote':'Assessment: actionable feature request.',
        'text':'자동화 봇 triage-helper[bot]은 요청을 처리 가능하다고 분류했다.'}]}, evidence,
        keys=('reaction',), strict=False, diagnostics={})
    assert valid['reaction'][0]['actor_type'] == 'bot'
    promoted = validate_extraction({'concerns':[{'source_id':'d2',
        'quote':'The setup is difficult.',
        'text':'프로젝트 메인테이너는 설정이 어렵다고 말했다.'}]}, evidence,
        keys=('concerns',), strict=False, diagnostics={})
    assert promoted['concerns'] == []


def test_developer_impact_requires_two_structured_source_categories():
    candidate = Candidate(Repository(1,'a/b','url',description='Official project description'))
    candidate.metrics['activity'] = Metric.known(12,'fixture')
    evidence = structure_evidence(candidate,[{'id':'d1','kind':'issues','source_type':'Issue',
        'url':'issue','title':'Workflow issue','text':'The workflow blocks on every prompt.'}])
    extraction = {
        'verified_facts':[{'source_id':'d1','quote':'The workflow blocks on every prompt.',
                           'text':'워크플로가 매 prompt에서 중단된다.'}],
        'developer_impact':[{
            'text':'Official project description과 The workflow blocks on every prompt. 근거는 개발 흐름에 영향을 줄 수 있다.',
            'source_ids':['d1','repository:description'],
            'derivation_type':'multi_source_synthesis',
            'grounding_terms':['Official project description','The workflow blocks on every prompt.']}],
    }
    diagnostics = {}
    result = validate_extraction(extraction,evidence,
        keys=('verified_facts','developer_impact'),strict=False,diagnostics=diagnostics)
    assert result['developer_impact'][0]['source_ids'] == ['d1','repository:description']
    assert result['developer_impact'][0]['urls'] == ['issue','url']
    assert result['developer_impact'][0]['derivation_type'] == 'multi_source_synthesis'
    assert diagnostics['accepted'] == 2

    rejected = validate_extraction({'developer_impact':[{'text':'근거가 하나뿐인 합성',
        'source_ids':['repository:description','repository:description'],
        'derivation_type':'multi_source_synthesis',
        'grounding_terms':['Official project description']}]},evidence,
        keys=('developer_impact',),strict=False,diagnostics={})
    assert rejected['developer_impact'] == []


def test_developer_impact_rejects_unbacked_capability_and_accepts_grounded_synthesis():
    candidate = Candidate(Repository(1,'a/b','repo',description='Local Operator runs on your machine'))
    candidate.metrics['activity'] = Metric.known(12,'fixture')
    evidence = structure_evidence(candidate,[{'id':'d1','kind':'issues','source_type':'Issue',
        'url':'issue','title':'Subscriptions','text':'Use your existing AI subscriptions.'}])
    verified = {'source_id':'d1','quote':'Use your existing AI subscriptions.',
                'text':'기존 AI 구독을 사용한다.'}
    common = {'source_ids':['d1','repository:description'],
              'derivation_type':'multi_source_synthesis',
              'grounding_terms':['AI subscriptions','Local Operator']}
    accepted = validate_extraction({'verified_facts':[verified], 'developer_impact':[dict(common,
        text='Local Operator와 AI subscriptions 근거는 개발자의 도구 선택에 영향을 줄 수 있다.')]},
        evidence, keys=('verified_facts','developer_impact'), strict=False, diagnostics={})
    assert len(accepted['developer_impact']) == 1

    diagnostics = {}
    rejected = validate_extraction({'verified_facts':[verified], 'developer_impact':[dict(common,
        text='Local Operator와 AI subscriptions는 한국어 합성 기능을 제공해 개발에 영향을 준다.')]},
        evidence, keys=('verified_facts','developer_impact'), strict=False, diagnostics=diagnostics)
    assert rejected['developer_impact'] == []
    assert diagnostics['reasons']['impact_not_grounded'] == 1


def test_issue_is_not_promoted_to_trending_reason_and_signals_are_separate():
    candidate = Candidate(Repository(1, 'a/b', 'url'))
    candidate.metrics['activity'] = Metric.known(19, 'weekly exact count')
    candidate.events.append(Event('issue', 'Important bug', 'issue',
        '2026-01-05T00:00:00+00:00', 'issues'))
    evidence = structure_evidence(candidate, [{'id':'d1','kind':'issues','source_type':'Issue',
        'url':'issue','title':'Important bug','text':'Important bug report'}])
    diagnostics = {}
    result = validate_extraction({'reason_for_trending':[{'source_id':'d1',
        'quote':'Important bug report','text':'이 Issue가 화제의 원인이다.'}]}, evidence,
        keys=('reason_for_trending',), strict=False, diagnostics=diagnostics)
    assert result['reason_for_trending'] == []
    assert diagnostics['reasons'] == {'unsupported_source_type':1}
    assert evidence['reason_for_trending'] == []
    assert evidence['notable_events'][0]['text'] == 'Important bug'
    assert evidence['attention_signals'][0]['metric'] == 'activity'


def test_all_confirmed_attention_metrics_are_deterministic():
    candidate = Candidate(Repository(1, 'a/b', 'url'))
    for name,value in {'trending_rank':2,'star_delta':120,'star_growth':12.5,
        'issues':12,'pulls':7,'issues_growth':50,'pulls_growth':40}.items():
        candidate.metrics[name] = Metric.known(value, 'fixture')
    candidate.events.extend([
        Event('patch','v0.1.1','release/1','2026-01-05T00:00:00+00:00','release'),
        Event('patch','v0.1.2','release/2','2026-01-06T00:00:00+00:00','release'),
    ])
    evidence = structure_evidence(candidate, [])
    names = {item['metric'] for item in evidence['attention_signals']}
    assert names == {'trending_rank','star_delta','star_growth','issues','pulls',
                     'issues_growth','pulls_growth','release_activity'}
    assert evidence['reason_for_trending'] == []
