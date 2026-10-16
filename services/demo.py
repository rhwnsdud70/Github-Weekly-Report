"""Deterministic synthetic fixtures. Every HTTP call stays inside MockTransport."""
import base64
from collections import Counter
from dataclasses import asdict
from datetime import timedelta
import json
import re

import httpx

from collectors.github_client import GitHubClient
from collectors.github_repository import repository_model
from config import rules
from services.narrative_service import OpenAIJSONClient, NARRATIVE_KEYS
from telegram.sender import TelegramSender


class DemoBackend:
    def __init__(self, period, count=10, failed_message=None):
        self.period, self.count = period, count
        self.failed_message = failed_message
        self.calls = Counter()
        self.deep_repositories = set()
        self.sent = []
        self.event_time = (period.end-timedelta(days=1)).isoformat()
        self.repositories = [self._repository(i) for i in range(1,count+1)]

    def _repository(self, number):
        name = f'synthetic-fixture/ai-tool-{number:02}'
        return {'id':900000+number,'full_name':name,'html_url':'https://github.com/'+name,
            'description':'합성 Fixture: 개발자용 AI 실행 및 테스트 도구. 실제 프로젝트가 아닙니다.',
            'topics':['llm','developer-tools'],'language':'Python',
            'created_at':(self.period.end-timedelta(days=100)).isoformat(),
            'pushed_at':self.event_time,'stargazers_count':1000+(self.count-number)*100,
            'forks_count':200+number,'fork':False}

    def seed_history(self, database):
        with database.transaction():
            for raw in self.repositories:
                repo = repository_model(raw)
                database.repository(repo.identity,asdict(repo))
                if database.previous_snapshot(repo.identity,self.period.start.isoformat()):
                    continue
                delta = 0 if raw is self.repositories[-1] else 500
                database.snapshot(repo.identity,self.period.start.isoformat(),{
                    'measured_at':self.period.start.isoformat(),'stars':repo.stars-delta,
                    'forks':repo.forks-20,'discussion_total':10})

    def _issue(self, name, number=1, pull=False):
        return {'number':number,'title':'합성 Fixture 개발 도구 평가',
            'body':'합성 Fixture: 로컬 캐시 기능을 검토합니다.',
            'html_url':f'https://github.com/{name}/'+('pull/' if pull else 'issues/')+str(number),
            'repository_url':f'https://api.github.com/repos/{name}',
            'created_at':self.event_time,'comments':25,'user':{'login':'fixture-user'},
            'author_association':'NONE'}

    def _documents(self, name, kind):
        if kind == 'readme':
            text = '합성 Fixture: 이 도구는 개발자가 AI 작업을 로컬에서 실행하고 결과를 검증하도록 돕습니다.'
        else:
            text = '합성 Fixture: 공식 문서는 로컬 캐시 설정과 검증 절차를 설명합니다.'
        return {'content':base64.b64encode(text.encode()).decode(),
                'html_url':f'https://github.com/{name}/blob/main/{kind}.md'}

    def respond(self, request):
        host,path = request.url.host,request.url.path
        self.calls[f'{host}{path}'] += 1
        if host == 'github.com' and path == '/trending':
            return httpx.Response(200,text=''.join(f'<article><h2><a href="/{r["full_name"]}">Fixture</a></h2></article>'
                for r in self.repositories))
        if host == 'api.openai.com':
            return self._openai(json.loads(request.content))
        if host == 'api.telegram.org':
            body = json.loads(request.content)
            self.sent.append(body)
            if self.failed_message and self.failed_message in body['text']:
                return httpx.Response(503,json={'ok':False,'error_code':503})
            return httpx.Response(200,json={'ok':True,'result':{'message_id':len(self.sent)}})
        if host != 'api.github.com':
            raise AssertionError('Unexpected fixture HTTP host')
        if path == '/search/repositories':
            query = request.url.params['q']
            topic = re.search(r'topic:([^ ]+)',query).group(1)
            index = rules.SEARCH_TOPICS.index(topic)
            size = int(request.url.params['per_page'])
            start = round(self.count*.4)+index*size
            items = [self.repositories[i%self.count] for i in range(start,start+size)]
            return httpx.Response(200,json={'items':items,'total_count':len(items),'incomplete_results':False})
        if path == '/search/issues':
            query = request.url.params['q']
            match = re.search(r'repo:([^ ]+)',query)
            if not match:
                # Adds event discovery coverage, including names beyond the search allocation.
                items = [self._issue(repo['full_name']) for repo in self.repositories[-max(2,self.count//10):]]
                return httpx.Response(200,json={'items':items,'total_count':len(items),'incomplete_results':False})
            name = match.group(1)
            number = int(name.rsplit('-',1)[1])
            current = f'created:{self.period.start.isoformat()}' in query
            total = (40+number if current else 10) if number != self.count else 0
            size = int(request.url.params['per_page'])
            items = [self._issue(name,i,'is:pr' in query) for i in range(1,min(size,total)+1)]
            if size > 1:
                self.deep_repositories.add(name)
            return httpx.Response(200,json={'items':items,'total_count':total,'incomplete_results':False})
        if path == '/advisories':
            repo = self.repositories[-1]
            return httpx.Response(200,json=[{'severity':'critical','summary':'합성 Fixture Critical 취약점',
                'html_url':'https://github.com/advisories/SYNTHETIC-FIXTURE',
                'source_code_location':repo['html_url'],'published_at':self.event_time,
                'description':'합성 Fixture: 테스트용 보안 결함이며 실제 취약점이 아닙니다.',
                'vulnerabilities':[{'vulnerable_version_range':'<2.0.1','first_patched_version':'2.0.1'}]}])
        if path == '/graphql':
            payload = json.loads(request.content)
            if 'createdAt' in payload['query'] and any(key.startswith('r0_') for key in payload['variables']):
                result = {}
                for alias, query in payload['variables'].items():
                    current = self.period.start.date().isoformat() in query
                    count = (7 if alias.endswith('_pr') else 12) if current else (2 if alias.endswith('_pr') else 3)
                    occurred_at = self.event_time if current else (self.period.start-timedelta(days=1)).isoformat()
                    result[alias] = {'nodes':[{'createdAt':occurred_at} for _ in range(count)],
                                     'pageInfo':{'hasNextPage':False}}
                return httpx.Response(200, json={'data': result})
            name = payload['variables']['owner']+'/'+payload['variables']['name']
            if 'totalCount' in payload['query']:
                count = 10 if name == self.repositories[-1]['full_name'] else 20
                return httpx.Response(200,json={'data':{'repository':{'discussions':{'totalCount':count}}}})
            self.deep_repositories.add(name)
            discussion = {'title':'합성 Fixture 캐시 사용 경험','body':'합성 Fixture: 캐시 사용 방법을 논의합니다.',
                'url':f'https://github.com/{name}/discussions/1','createdAt':self.event_time,
                'updatedAt':self.event_time,'author':{'login':'fixture-user'},'comments':{'nodes':[]}}
            return httpx.Response(200,json={'data':{'repository':{'discussions':{'nodes':[discussion]}}}})
        match = re.match(r'/repos/([^/]+/[^/]+)(.*)',path)
        if match:
            name,suffix = match.groups()
            raw = next(r for r in self.repositories if r['full_name']==name)
            if not suffix:
                return httpx.Response(200,json=raw)
            if suffix in ('/readme','/contents/docs/index.md'):
                self.deep_repositories.add(name)
                return httpx.Response(200,json=self._documents(name,'readme' if suffix=='/readme' else 'docs'))
            if suffix == '/releases':
                releases = [] if raw is self.repositories[-1] else [{'tag_name':'v2.0.0',
                    'name':'합성 Fixture v2.0 정식 릴리스','body':'합성 Fixture: 로컬 캐시 기능과 검증 API가 추가되었습니다.',
                    'html_url':f'https://github.com/{name}/releases/tag/v2.0.0','published_at':self.event_time}]
                return httpx.Response(200,json=releases)
            if suffix.endswith('/comments'):
                return httpx.Response(200,json=[{'body':'합성 Fixture: 한 사용자는 캐시 설정이 편리하다고 평가합니다. 베타 API 변경 가능성을 우려합니다.',
                    'html_url':f'https://github.com/{name}{suffix}#comment-1',
                    'created_at':self.event_time,'user':{'login':'fixture-user'}}])
        raise AssertionError(f'Unexpected fixture HTTP route: {path}')

    def _openai(self, request):
        evidence = json.loads(request['input'])
        properties = request['text']['format']['schema']['properties']
        first_property = next(iter(properties.values()))
        extraction = 'quote' in first_property.get('items', {}).get('properties', {})
        if extraction:
            result = {key:[] for key in properties}
            choices = {'verified_facts':'Issue','reaction':'IssueComment',
                'positive_opinions':'IssueComment','concerns':'IssueComment','risks':'Security'}
            for key,source_type in choices.items():
                document = next((d for d in evidence['documents']
                                 if d.get('source_type')==source_type),None)
                if document:
                    result[key] = [{'source_id':document['source_id'],'quote':document['text'],'text':document['text']}]
            verified = next((d for d in evidence['documents'] if d.get('source_type')=='Issue'),None)
            project = next(iter(evidence.get('deterministic_support',{}).get('project_summary',[])),None)
            if verified and project:
                verified_term = verified['text']
                project_term = project['text']
                result['developer_impact'] = [{
                    'text':f'{project_term} 및 {verified_term} 근거는 개발 업무에 영향을 줄 수 있다.',
                    'source_ids':[verified['source_id'],project['source_id']],
                    'derivation_type':'multi_source_synthesis',
                    'grounding_terms':[verified_term,project_term]}]
        else:
            result = {key:[] for key in NARRATIVE_KEYS}
            choices = {'background':'verified_facts','trigger':'reason_for_trending',
                'attention':'attention_signals','reaction':'reaction','project':'project_summary',
                'impact':'developer_impact','positive':'positive_opinions',
                'concerns':'concerns','debates':'debates','risks':'risks'}
            for key,evidence_key in choices.items():
                item = next((item for item in evidence['structured_evidence'][evidence_key]
                             if item.get('source_id') or item.get('source_ids')),None)
                if item:
                    ids = item.get('source_ids') or [item['source_id']]
                    result[key] = [{'text':item['text'],'source_ids':ids}]
        return httpx.Response(200,json={'status':'completed','output':[{'content':[
            {'type':'output_text','text':json.dumps(result,ensure_ascii=False)}]}]})

    def clients(self):
        client = httpx.Client(transport=httpx.MockTransport(self.respond))
        github = GitHubClient(client=client,sleep=lambda _:None)
        llm = OpenAIJSONClient('offline-fixture','fixture-model',client,sleep=lambda _:None)
        sender = TelegramSender('offline-fixture','fixture-chat',client,sleep=lambda _:None)
        return client,github,llm,sender
