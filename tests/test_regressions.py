from datetime import timedelta
from collectors.github_repository import repository_model
from collectors.github_issue import issue_event
from models.domain import Candidate, Repository, Metric
from services.metric_service import calculate_metrics
from tests.test_ranking import period


def test_no_accidental_ai_substring_and_no_homework():
    raw = {'id':1,'full_name':'owner/chair','html_url':'url','description':'A chair collection'}
    assert not repository_model(raw).relevant
    raw['description'] = 'AI homework assignment'
    assert repository_model(raw).excluded_reason


def test_official_policy_changes_not_user_requests():
    item = {'title':'License changed to MIT','html_url':'url','created_at':period().start.isoformat()}
    assert issue_event(item).kind == 'discussion'
    item['author_association'] = 'OWNER'
    assert issue_event(item).kind == 'license'
    item['title'] = 'Could the license change to MIT?'
    assert issue_event(item).kind == 'discussion'


def test_new_repository_and_discussion_snapshots():
    repo = Repository(1,'a/b','url',stars=500,created_at=(period().end-timedelta(days=14)).isoformat())
    c = Candidate(repo,metrics={'discussion_total':Metric.known(20)},previous={'snapshot':{'discussion_total':10}})
    calculate_metrics(c,period())
    assert c.metric('new_repository_priority').value is True
    assert c.metric('discussion_delta').value == 10
    assert c.metric('discussion_growth').value == 100


def test_release_combined_conditions_choose_highest():
    from collectors.github_release import release_event
    event = release_event({'tag_name':'v2.0.0','body':'## Breaking changes\nA documented migration.',
        'html_url':'url','published_at':period().start.isoformat()})
    assert event.kind == 'breaking'
    assert 'major' in event.details['classification_matches']


def test_official_high_rce_is_mandatory_critical():
    from collectors.github_security import security_event
    event = security_event({'severity':'high','summary':'Remote code execution in parser',
        'html_url':'url','published_at':period().start.isoformat(),
        'cvss':{'vector_string':'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'}})
    c = Candidate(Repository(1,'a/b','url'),events=[event])
    assert c.critical
    assert event.kind == 'rce'
    assert event.details['easy_to_exploit']
