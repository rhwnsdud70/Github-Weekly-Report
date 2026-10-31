"""Phase 4 offline entrypoint. No production switch or environment loading."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Boundary observation: offline preparation only')
    parser.add_argument('--mock',action='store_true',help='Run in-memory synthetic observation only')
    args = parser.parse_args(argv)
    if not args.mock:
        print('Boundary observation: DISABLED; Phase 4 offline-only; use --mock')
        return 0
    return mock_run()


def mock_run():
    """All HTTP and R2 transports are in memory; no Settings/.env/credentials."""
    from datetime import datetime, timedelta, timezone
    import io
    from types import SimpleNamespace
    import httpx
    from models.observation import Producer
    from services.observation_pool import PoolRecord, build_pool
    from services.observation_pool_validation import seal_pool, encode_pool
    from services.observation_job import ObservationGitHub, run_boundary_observation
    from services.r2_observation_store import R2ObservationStore

    boundary = datetime(2026,11,1,10,tzinfo=timezone.utc)
    now = boundary+timedelta(minutes=1)
    producer = Producer('synthetic/observation','1',1,'a'*40)
    pool = seal_pool(build_pool([PoolRecord(1,'synthetic/example',boundary,boundary,boundary,
        verified=True,fixture=True)],as_of=boundary,fixture=True),
        pool_id='00000000-0000-4000-8000-000000000001',created_at=boundary,producer=producer)
    class MemoryS3:
        def __init__(self):
            self.handlers = {}
            self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts':1}),events=self)
            self.objects = {'test-observation-pools/v1/2026-11-01/'+pool.payload.pool_id+'.json':encode_pool(pool)}
        def register(self,event,handler,unique_id): self.handlers[unique_id] = handler
        def unregister(self,event,unique_id): self.handlers.pop(unique_id,None)
        def fire(self):
            for handler in self.handlers.values(): handler()
        def list_objects_v2(self,**kw):
            self.fire()
            return {'IsTruncated':False,'Contents':[{'Key':k,'LastModified':boundary}
                for k in self.objects if k.startswith(kw['Prefix'])]}
        def get_object(self,**kw):
            self.fire()
            raw = self.objects[kw['Key']]
            return {'Body':io.BytesIO(raw),'ContentLength':len(raw),'LastModified':now}
        def put_object(self,**kw):
            self.fire()
            if kw['IfNoneMatch'] != '*' or kw['Key'] in self.objects: raise RuntimeError('Immutable conflict')
            self.objects[kw['Key']] = kw['Body']
            return {}
    def respond(request):
        if request.url.host == 'github.com':
            return httpx.Response(200,text='<h2><a href="/synthetic/example">example</a></h2>')
        return httpx.Response(200,json={'id':1,'full_name':'synthetic/example',
            'html_url':'https://github.com/synthetic/example','created_at':'2020-01-01T00:00:00Z',
            'stargazers_count':123,'forks_count':4})
    clock = SimpleNamespace(now=lambda:now,monotonic=lambda:0,sleep=lambda _:None)
    settings = SimpleNamespace(validate_r2=lambda:None,r2_bucket='synthetic')
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with R2ObservationStore(settings,as_of=now,namespace='fixture',profile='observation_job',client=MemoryS3()) as store:
            github = ObservationGitHub(http,clock=clock)
            result = run_boundary_observation(store=store,github=github,producer=producer,clock=clock)
            print('Mode: MOCK; namespace=fixture; no external APIs')
            print('Boundary:',result.boundary.isoformat())
            print('Job:',result.status)
            print('Coverage:',result.envelope.payload.boundary_coverage if result.envelope else 'NONE')
            print('Publication:',result.receipt.publication_status if result.receipt else 'NOT_PUBLISHED')
            print('REST attempts:',github.rest_attempts,'Trending attempts:',github.trending_attempts)
            print('R2 logical requests:',store.logical_requests,'physical attempts:',store.physical_attempts)
            for category in result.diagnostics: print('Diagnostic:',category)
            return 0 if result.status == 'SUCCESS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
