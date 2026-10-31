"""Explicit SYNTHETIC data only. Production algorithms, in-memory transports.

Fixture envelopes remain in test-observations/test-observation-pools throughout.
The fake database client shares their in-memory bucket to detect key collisions.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from uuid import uuid5, NAMESPACE_URL

import httpx

from database.db import Database
from models.observation import Producer
from services.demo import DemoBackend
from services.observation_pool import PoolRecord, build_pool
from services.observation_pool_validation import seal_pool
from services.observation_job import ObservationGitHub, run_boundary_observation
from services.observation_consumer import consume_observations
from services.observation_pipeline import validate_pipeline_context
from services.r2_observation_store import R2ObservationStore
from services.r2_persistence import R2DatabaseStore, PRODUCTION_KEY
from services.pipeline import ReportPipeline
from services.weekly_live_runner import run_weekly_live_once
from tests.test_observation_validation import B
from tests.test_observation_consumer import PERIOD, CUTOFF
from tests.test_observation_job import VirtualClock
from tests.test_r2_observations import ObservationS3
from tests.test_r2_persistence import FakeS3
from tests.test_weekly_live_once import live_settings


SYNTHETIC_FIXTURE = True


class BucketObservationClient(ObservationS3):
    def __init__(self, objects, trace):
        super().__init__()
        self.objects, self.trace = objects,trace
        self.clock = VirtualClock()
        self.delayed_put = 0

    def send(self,operation,key):
        assert key.startswith(('test-observations/v1/','test-observation-pools/v1/'))
        self.trace.append(('observation',operation,key,self.clock.now()))
        super().send(operation,key)

    def put_object(self, **kwargs):
        try:
            response = super().put_object(**kwargs)
            if self.delayed_put and kwargs['Key'].startswith('test-observations/'):
                # A committed write can outlive a lost/slow acknowledgement.
                # The Job cannot preempt in-flight SDK I/O; its GET gate closes.
                self.clock.sleep(self.delayed_put)
            return response
        finally:
            if kwargs['Key'] in self.objects:
                self.modified[kwargs['Key']] = self.clock.now()


class BucketDatabaseClient(FakeS3):
    def __init__(self, data, trace):
        super().__init__(data)
        self.trace = trace

    def get_object(self, **kwargs):
        assert kwargs['Key'] == PRODUCTION_KEY
        self.trace.append(('database','get',kwargs['Key']))
        return super().get_object(**kwargs)

    def copy_object(self, **kwargs):
        assert kwargs['Key'].startswith('backups/')
        self.trace.append(('database','copy',kwargs['Key']))
        return super().copy_object(**kwargs)

    def put_object(self, **kwargs):
        assert kwargs['Key'] == PRODUCTION_KEY
        self.trace.append(('database','put',kwargs['Key']))
        return super().put_object(**kwargs)


class ObservationE2E:
    def __init__(self, root, *, no_events=False, failed_message=None, strict_context=False):
        self.root = root
        self.trace,self.output,self.contexts,self.jobs,self.receipts = [],[],[],[],[]
        self.backend = DemoBackend(PERIOD,10,failed_message=failed_message)
        for raw in self.backend.repositories:
            raw['stargazers_count'],raw['forks_count'] = 1500,200
        original = self.backend.respond
        def respond(request):
            self.trace.append(('weekly-http',request.url.host,request.url.path))
            if no_events and (request.url.path == '/advisories' or request.url.path.endswith('/releases')):
                return httpx.Response(200,json=[])
            return original(request)
        self.backend.respond = respond
        source = root/'initial.db'
        database = Database(source)
        database.connection.execute('PRAGMA journal_mode=WAL')
        database.close()
        self.initial = source.read_bytes()
        self.remote = BucketDatabaseClient(self.initial,self.trace)
        self.observation = BucketObservationClient(self.remote.objects,self.trace)
        self.clock = VirtualClock(4*3600)
        self.config = live_settings(root/'local'/'github_weekly_report.db')
        self.consumer_calls = 0
        self.strict_context = strict_context
        self.http_clients = []

    def produce(self, boundary, *, stars=1200, forks=150, sequence=1,
                fault=None, delay=60, slow_publication=0):
        clock = VirtualClock()
        clock.wall = boundary+timedelta(seconds=10)
        self.observation.clock = clock
        records = [PoolRecord(raw['id'],raw['full_name'],boundary,boundary,boundary,
                              source_kind='watchlist',verified=True,fixture=True,watchlisted_at=boundary)
                   for raw in self.backend.repositories]
        if fault == 'pool':
            from dataclasses import replace
            records[0] = replace(records[0],last_seen_at=boundary+timedelta(days=1))
        pool = build_pool(records,as_of=clock.now(),fixture=True)
        assert len(pool.entries) == 10
        pool_id = str(uuid5(NAMESPACE_URL,f'synthetic-pool:{boundary}:{sequence}'))
        producer = Producer('synthetic/weekly-report',str(sequence),1,'a'*40)
        envelope = seal_pool(pool,pool_id=pool_id,created_at=clock.now(),producer=producer)
        with R2ObservationStore(self.config,as_of=clock.now(),namespace='fixture',client=self.observation) as store:
            pool_receipt = store.publish_pool(envelope)
        assert pool_receipt.publication_status == 'VERIFIED'
        self.receipts.append(pool_receipt)
        clock.wall = boundary+timedelta(seconds=delay)
        self.observation.put_mode = 'drop' if fault == 'put' else 'commit-timeout' if fault == 'commit-timeout' else None
        self.observation.delayed_put = slow_publication
        def metadata(request):
            clock.sleep(0.1)
            if request.url.host == 'github.com':
                return httpx.Response(200,text='changed markup' if fault == 'trending' else ''.join(
                    f'<h2><a href="/{raw["full_name"]}">synthetic</a></h2>' for raw in self.backend.repositories))
            assert request.url.path.startswith('/repositories/')
            identity = int(request.url.path.rsplit('/',1)[1])
            if fault == 'metadata' or (fault == 'one-metadata' and identity == 900001):
                return httpx.Response(404)
            raw = deepcopy(next(r for r in self.backend.repositories if r['id'] == identity))
            # Values are responses from this synthetic clock, not backdated live data.
            raw['stargazers_count'] = stars[identity] if isinstance(stars,dict) else stars
            raw['forks_count'] = forks
            raw['pushed_at'] = (boundary-timedelta(days=1)).isoformat()
            return httpx.Response(200,json=raw,headers={'x-ratelimit-remaining':'4990','x-ratelimit-resource':'core'})
        object_id = str(uuid5(NAMESPACE_URL,f'synthetic-observation:{boundary}:{sequence}'))
        try:
            with httpx.Client(transport=httpx.MockTransport(metadata)) as http:
                github = ObservationGitHub(http,clock=clock)
                with R2ObservationStore(self.config,as_of=clock.now(),namespace='fixture',
                        profile='observation_job',client=self.observation) as store:
                    result = run_boundary_observation(store=store,github=github,producer=producer,
                                                       clock=clock,observation_id=object_id)
                    assert github.rest_attempts <= 45 and github.trending_attempts <= 2
                    assert store.logical_requests <= 10 and store.physical_attempts <= 30
            self.jobs.append(result)
            return result
        finally:
            self.observation.put_mode = None
            self.observation.delayed_put = 0

    def both(self, *, previous=1000, current=1200, **kwargs):
        if previous is not None: self.produce(PERIOD.start,stars=previous,forks=100)
        if current is not None: self.produce(PERIOD.end,stars=current,forks=150,**kwargs)
        return self

    @contextmanager
    def pipeline_factory(self, database):
        client,github,llm,sender = self.backend.clients()
        self.http_clients.append(client)
        def load(period,cutoff,legacy):
            assert database.rows("SELECT status FROM automation_runs WHERE mode='live'") == [{'status':'RUNNING'}]
            self.trace.append(('consumer','after-claim',cutoff))
            self.consumer_calls += 1
            self.observation.clock = self.clock
            with R2ObservationStore(self.config,as_of=cutoff,namespace='fixture',
                                    profile='report_read',client=self.observation) as store:
                # Only bytes from SDK GETs are consumed; Job results are never input.
                context = consume_observations(store=store,period=period,cutoff=cutoff,
                    legacy_snapshots=legacy,prefer_object_snapshots=True)
                assert store.logical_requests <= 18 and store.physical_attempts <= 54
            self.contexts.append(context)
            return context
        try:
            validator = (validate_pipeline_context if self.strict_context else
                         lambda ctx,period,cutoff:validate_pipeline_context(ctx,period,cutoff,namespace='fixture'))
            yield ReportPipeline(database,github,client,llm,sender,clock=self.clock.now,
                observation_loader=load,observation_validator=validator)
        finally:
            client.close()

    def run(self, *, config=None):
        config = config or self.config
        return run_weekly_live_once(config,clock=self.clock.now,
            store=R2DatabaseStore(config,self.remote),pipeline_factory=self.pipeline_factory,
            output=self.root/'artifacts',emit=self.output.append)

    def replay(self):
        # New empty local path represents a fresh Actions host; the persisted
        # remote Claim must block work before pipeline_factory is entered.
        from dataclasses import replace
        return self.run(config=replace(self.config,database_path=str(self.root/'replay'/'github_weekly_report.db')))

    def candidates(self):
        path = next((self.root/'artifacts').rglob('candidates.json'))
        return json.loads(path.read_text(encoding='utf-8'))['candidates']

    def remote_database(self, name='inspect.db'):
        path = self.root/name
        path.write_bytes(self.remote.objects[PRODUCTION_KEY])
        return Database(path)

    def observation_bytes(self):
        return {k:v for k,v in self.remote.objects.items() if k.startswith(('test-observations/','test-observation-pools/'))}
