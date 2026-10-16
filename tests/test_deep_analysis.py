import base64
import pytest
from collectors.github_client import GitHubError
from models.domain import Event
from services.deep_analysis_service import DeepAnalysisCollector
from tests.test_ranking import sample_candidates, period


def test_only_top_and_bounded_collection():
    class Fake:
        def request(self, path, *args):
            if path.endswith('readme'):
                return {'content':base64.b64encode(b'Official README').decode(), 'html_url':'https://github.com/a/b'}
            raise GitHubError('unavailable')
        def issues(self, name, query, limit):
            assert limit == 3
            return []
        def pulls(self, name, query, limit):
            assert limit == 2
            return []
        def discussions(self, name, limit):
            assert limit == 2
            return []
    collector = DeepAnalysisCollector(Fake())
    with pytest.raises(ValueError):
        collector.collect_top(sample_candidates(6), period())
    documents = collector.collect_top(sample_candidates(5), period())
    assert len(documents) == 5
    assert all(len(items) == 1 for items in documents.values())


def test_comment_types_and_release_period_are_preserved():
    current = period()
    candidate = sample_candidates(1)[0]
    candidate.events = [
        Event('major','Inside','release/in',current.start.isoformat(),'release',{'body':'inside'}),
        Event('major','At end','release/end',current.end.isoformat(),'release',{'body':'outside'}),
    ]
    class Fake:
        def request(self, path, *args):
            if path.endswith('readme') or path.endswith('docs/index.md'):
                raise GitHubError('unavailable')
            if path.endswith('/comments'):
                return [{'body':'Useful feedback','html_url':path+'#1',
                    'created_at':current.start.isoformat(),'user':{'login':'person'}}]
            raise GitHubError('unavailable')
        def issues(self, *args):
            return [{'number':1,'title':'Issue','body':'Issue body','html_url':'issue/1',
                'created_at':current.start.isoformat(),'user':{'login':'person'}}]
        def pulls(self, *args):
            return [{'number':2,'title':'PR','body':'PR body','html_url':'pull/2',
                'created_at':current.start.isoformat(),'user':{'login':'person'}}]
        def discussions(self, *args):
            return []
    documents = DeepAnalysisCollector(Fake()).collect(candidate, current)
    types = [document['source_type'] for document in documents]
    assert 'IssueComment' in types
    assert 'PRComment' in types
    assert [document['title'] for document in documents if document['source_type']=='Release'] == ['Inside']
    assert candidate.deep_analysis_diagnostics['reasons']['outside_period'] == 1


def test_patch_releases_and_similar_comments_are_representative():
    current = period()
    candidate = sample_candidates(1)[0]
    candidate.events = [Event('patch',f'v0.1.{number}',f'release/{number}',
        current.start.isoformat(),'release',{'body':f'Patch {number}'}) for number in range(6)]
    class Fake:
        def request(self, path, *args):
            if path.endswith('readme') or path.endswith('docs/index.md'):
                raise GitHubError('unavailable')
            if path.endswith('/comments'):
                return [
                    {'body':'Cache setup is useful and fast','html_url':path+'#1',
                     'created_at':current.start.isoformat(),'user':{'login':'one'}},
                    {'body':'Cache setup is useful and very fast','html_url':path+'#2',
                     'created_at':current.start.isoformat(),'user':{'login':'two'}},
                    {'body':'The API name is confusing','html_url':path+'#3',
                     'created_at':current.start.isoformat(),'user':{'login':'three'}},
                    {'body':'A fourth unrelated observation','html_url':path+'#4',
                     'created_at':current.start.isoformat(),'user':{'login':'four'}},
                ]
            raise GitHubError('unavailable')
        def issues(self, *args):
            return [{'number':1,'title':'Issue','body':'Body','html_url':'issue/1',
                'created_at':current.start.isoformat(),'user':{'login':'person'}}]
        def pulls(self, *args): return []
        def discussions(self, *args): return []
    documents = DeepAnalysisCollector(Fake()).collect(candidate,current)
    assert sum(document['source_type']=='Release' for document in documents) == 1
    comments = [document for document in documents if document['source_type']=='IssueComment']
    assert len(comments) == 2
    assert any('confusing' in document['text'] for document in comments)


def test_github_actor_metadata_is_preserved_for_bot_classification():
    current = period()
    candidate = sample_candidates(1)[0]
    class Fake:
        def request(self, path, *args):
            if path.endswith('readme') or path.endswith('docs/index.md'):
                raise GitHubError('unavailable')
            if path.endswith('/comments'):
                return [{'body':'Automated triage result','html_url':path+'#1',
                    'created_at':current.start.isoformat(),
                    'author_association':'NONE','user':{'login':'triage[bot]','type':'Bot'}}]
            raise GitHubError('unavailable')
        def issues(self, *args):
            return [{'number':1,'title':'Issue','body':'Issue body','html_url':'issue/1',
                'created_at':current.start.isoformat(),'author_association':'NONE',
                'user':{'login':'person','type':'User'}}]
        def pulls(self, *args): return []
        def discussions(self, *args): return []
    documents = DeepAnalysisCollector(Fake()).collect(candidate,current)
    comment = next(item for item in documents if item['source_type']=='IssueComment')
    assert comment['author'] == 'triage[bot]'
    assert comment['github_actor_type'] == 'Bot'


@pytest.mark.parametrize('status,category',[(404,'HTTP_ERROR'),(403,'RATE_LIMIT'),(500,'HTTP_ERROR')])
def test_optional_docs_failures_keep_safe_endpoint_diagnostics(status,category):
    import httpx
    from collectors.github_client import GitHubClient
    from services.evidence_service import structure_evidence
    candidate = sample_candidates(1)[0]
    requests = []
    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith('/readme'):
            return httpx.Response(200,json={'content':base64.b64encode(b'Official README').decode()})
        if request.url.path.endswith('/contents/docs/index.md'):
            headers = {'x-ratelimit-remaining':'0'} if status == 403 else {}
            return httpx.Response(status,headers=headers,json={'message':'Not printed private response'})
        if request.url.path == '/graphql':
            return httpx.Response(200,json={'data':{'repository':{'discussions':{'nodes':[]}}}})
        return httpx.Response(200,json={'items':[]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = GitHubClient('private-test-token',transport,sleep=lambda _:None)
        documents = DeepAnalysisCollector(client).collect(candidate,period())
    structure_evidence(candidate,documents)
    failure = next(item for item in candidate.deep_analysis_diagnostics['source_failures']
                   if item['source']=='official docs')
    assert failure['endpoint'] == f'repos/{candidate.repository.full_name}/contents/docs/index.md'
    assert failure['source_type'] == 'OfficialDocs'
    assert failure['category'] == category
    assert failure['http_status'] == status
    assert failure['optional'] is True
    assert any(document['source_type']=='README' for document in documents)
    assert 'private-test-token' not in str(failure)
    assert 'Not printed private response' not in str(failure)
    assert candidate.evidence['validation_diagnostics']['collection']['source_failures']
