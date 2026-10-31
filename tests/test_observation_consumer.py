from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import socket

import pytest

from models.domain import ReportPeriod
from models.observation import PoolReference, TrendingEntry
from models.observation_context import LegacySnapshot, LatestMetadata
from services.observation_consumer import consume_observations, ObservationConsumerError
from services.observation_validation import seal_observation, encode_observation
from services.r2_observation_store import R2ObservationStore
from tests.test_observation_validation import B, OBS_ID, payload
from tests.test_r2_observations import ObservationS3
from tests.test_r2_persistence import settings, FakeClientError, PRODUCTION_KEY


ID = 900001
PERIOD = ReportPeriod(B-timedelta(days=7),B)
CUTOFF = B+timedelta(hours=4)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a,**kw): pytest.fail('External networking forbidden')
    monkeypatch.setattr(socket,'create_connection',blocked)
    monkeypatch.setattr(socket.socket,'connect',blocked)


def observation(boundary=B, *, index=1, delay=60, stars=1200, forks=80, request_delay=None):
    p = payload(delay)
    offset = boundary-B
    row = p.repositories[0]
    request = boundary+timedelta(seconds=max(0,delay-1) if request_delay is None else request_delay)
    response = boundary+timedelta(seconds=delay)
    row = replace(row,request_started_at=request,response_received_at=response,observed_at=response,
                  stars=stars,forks=forks,diagnostics=replace(row.diagnostics,
                  duration_ms=round((response-request).total_seconds()*1000)))
    page = replace(p.trending,request_started_at=request,response_received_at=response,observed_at=response,
                   diagnostics=row.diagnostics)
    from services.observation_timing import observation_allowed
    eligible = observation_allowed(request,boundary) and observation_allowed(response,boundary)
    return replace(p,observation_id=f'00000000-0000-4000-8000-{index:012d}',period_start=p.period_start+offset,
        boundary_end=boundary,started_at=request,completed_at=response+timedelta(seconds=1),
        pool_ref=PoolReference('test-observation-pools/v1/'+boundary.date().isoformat()+'/'+OBS_ID+'.json','b'*64),
        repositories=(row,),trending=page,boundary_coverage='FULL' if eligible else 'NONE')


def seed(s3, p, *, modified=None):
    key = 'test-observations/v1/'+p.boundary_end.date().isoformat()+'/'+p.observation_id+'.json'
    s3.objects[key] = encode_observation(seal_observation(p))
    s3.modified[key] = modified or p.completed_at
    return key


def consume(tmp_path, s3, **kwargs):
    with R2ObservationStore(settings(tmp_path/'untouched.db'),as_of=CUTOFF,
                           namespace=kwargs.pop('namespace','fixture'),profile='report_read',client=s3) as store:
        result = consume_observations(store=store,period=PERIOD,cutoff=CUTOFF,**kwargs)
        assert store.logical_requests <= 18 and store.physical_attempts <= 54
        assert all(call in {'list','get'} for call in s3.calls)
        return result


def both():
    s3 = ObservationS3()
    seed(s3,observation(B-timedelta(days=7),stars=1000,forks=50))
    seed(s3,observation())
    return s3


def test_normal_comparison_delayed_consumer_latest_separated_and_deeply_frozen(tmp_path):
    s3 = both()
    meta = observation().repositories[0].metadata
    latest = LatestMetadata(meta,CUTOFF,1500,90,fixture=True)
    original = deepcopy(s3.objects)
    context = consume(tmp_path,s3,latest_metadata=(latest,))
    row = context.for_repository(ID)
    assert row.current.snapshot.stars == 1200 and row.previous.snapshot.stars == 1000
    assert row.latest.stars == 1500
    assert row.comparison.star_delta.value == 200
    assert row.comparison.star_growth.value == 20
    assert row.comparison.fork_delta.value == 30
    assert row.comparison.fork_growth.value == 60
    assert row.trending_rank.value == 1
    assert context.summary.star_comparisons_confirmed == 1
    assert context.summary.conflicted_repositories == 0
    assert row.current.snapshot.provenance.observed_at == B+timedelta(seconds=60)
    assert len(row.current.snapshot.provenance.sha256) == 64
    assert s3.objects == original
    assert not (tmp_path/'untouched.db').exists()
    with pytest.raises(FrozenInstanceError): row.current.snapshot.stars = 999
    with pytest.raises(FrozenInstanceError): row.latest.metadata.full_name = 'untrusted/name'


@pytest.mark.parametrize('missing',['current','previous','both'])
def test_missing_boundary_unknown_not_zero(tmp_path,missing):
    s3 = ObservationS3()
    if missing not in {'current','both'}: seed(s3,observation())
    if missing not in {'previous','both'}: seed(s3,observation(B-timedelta(days=7)))
    context = consume(tmp_path,s3,repository_ids=(ID,))
    row = context.for_repository(ID)
    assert row.comparison.star_delta.status == 'unknown'
    assert row.comparison.star_growth.status == 'unknown'
    assert 'OBSERVATION_MISSING' in {d.category for d in context.diagnostics}
    assert not any(d.category == 'OBSERVATION_STORE_UNAVAILABLE' for d in context.diagnostics)


@pytest.mark.parametrize('old,new,delta,status',[(1300,1200,-100,'confirmed'),(0,1200,1200,'not_applicable'),
                                                (1000,1000,0,'confirmed')])
def test_decrease_zero_previous_and_actual_zero(tmp_path,old,new,delta,status):
    s3 = ObservationS3()
    seed(s3,observation(B-timedelta(days=7),stars=old))
    seed(s3,observation(stars=new))
    row = consume(tmp_path,s3).for_repository(ID)
    assert row.comparison.star_delta.value == delta
    assert row.comparison.star_growth.status == status


def test_earliest_response_not_highest_or_latest_and_actual_interval(tmp_path):
    s3 = both()
    seed(s3,observation(index=2,delay=600,stars=1800))
    row = consume(tmp_path,s3).for_repository(ID)
    assert row.current.snapshot.stars == 1200
    assert row.comparison.star_delta.value == 200
    # Another actual earlier current response changes observed interval honestly.
    s3 = both()
    seed(s3,observation(index=2,delay=30,stars=1100))
    row = consume(tmp_path,s3).for_repository(ID)
    assert row.comparison.observed_interval_seconds == 168*3600-30


def test_tie_request_then_object_id_is_deterministic_and_idempotent(tmp_path):
    s3 = both()
    seed(s3,observation(index=2,delay=60,request_delay=58))
    result = consume(tmp_path,s3)
    row = result.for_repository(ID)
    assert row.current.snapshot.provenance.object_id.endswith('000000000002')
    assert len(result.repositories) == 1
    # Equal requests fall back to lexical object ID, independent of list order.
    s3 = both()
    seed(s3,observation(index=2))
    assert consume(tmp_path,s3).for_repository(ID).current.snapshot.provenance.object_id.endswith('000000000001')


def test_same_time_conflict_is_unknown_and_preserves_conflict_provenance(tmp_path):
    s3 = both()
    seed(s3,observation(index=2,stars=1300))
    row = consume(tmp_path,s3).for_repository(ID)
    assert row.current.status == 'CONFLICT'
    assert len(row.current.conflict_sources) == 2
    assert row.comparison.star_delta.status == 'unknown'
    assert row.current.snapshot is None


def test_later_conflict_not_hidden_behind_earliest_good_value(tmp_path):
    s3 = both()
    seed(s3,observation(index=2,delay=100,stars=1400))
    seed(s3,observation(index=3,delay=100,stars=1500))
    assert consume(tmp_path,s3).for_repository(ID).current.status == 'CONFLICT'


def legacy(boundary=B,stars=1200,**kw):
    return LegacySnapshot(ID,boundary,boundary+timedelta(seconds=60),stars,80,
                          boundary+timedelta(seconds=59),fixture=True,**kw)


def test_legacy_valid_read_only_empty_r2_and_batch_precision(tmp_path):
    rows = (legacy(),legacy(B-timedelta(days=7),1000))
    before = deepcopy(rows)
    row = consume(tmp_path,ObservationS3(),legacy_snapshots=rows).for_repository(ID)
    assert row.comparison.star_delta.value == 200
    assert row.current.snapshot.provenance.timestamp_precision == 'batch_completion'
    assert row.current.snapshot.provenance.sha256 is None
    assert row.current.snapshot.provenance.object_key is None
    assert rows == before


def test_legacy_object_same_time_conflict(tmp_path):
    row = consume(tmp_path,both(),legacy_snapshots=(legacy(stars=999),)).for_repository(ID)
    assert row.current.status == 'CONFLICT'
    assert row.comparison.star_delta.status == 'unknown'


@pytest.mark.parametrize('change,reason',[
    ({'request_started_at':None},'LEGACY_REQUEST_TIME_MISSING'),
    ({'measured_at':B+timedelta(hours=2)},'LEGACY_OUTSIDE_WINDOW'),
    ({'stars':True},'INVALID_INTEGER'),
    ({'fixture':False},'FIXTURE_NAMESPACE_MISMATCH'),
    ({'boundary':B-timedelta(days=14)},'LEGACY_BOUNDARY_MISMATCH'),
])
def test_invalid_legacy_excluded_with_safe_reason(tmp_path,change,reason):
    context = consume(tmp_path,ObservationS3(),repository_ids=(ID,),legacy_snapshots=(replace(legacy(),**change),))
    assert reason in {d.category for d in context.diagnostics}
    assert context.for_repository(ID).current.status == 'MISSING'


@pytest.mark.parametrize('delay,eligible',[(0,True),(600,True),(3600,True),(3601,False),(14400,False)])
def test_boundary_window_and_no_retimestamp(tmp_path,delay,eligible):
    s3 = ObservationS3()
    # CUTOFF is4h; response+completion at4h+1s is not yet published at cutoff.
    seed(s3,observation(delay=delay))
    row = consume(tmp_path,s3,repository_ids=(ID,)).for_repository(ID)
    assert (row.current.status == 'CONFIRMED') == eligible
    if eligible: assert row.current.snapshot.provenance.observed_at == B+timedelta(seconds=delay)


def test_trending_whole_page_earliest_not_combined_and_unresolved_no_rank(tmp_path):
    s3 = both()
    later = observation(index=2,delay=600)
    entries = (TrendingEntry('other/unresolved',1),TrendingEntry('synthetic-fixture/repo',2,ID,'confirmed'))
    seed(s3,replace(later,trending=replace(later.trending,entries=entries,page_sha256='c'*64)))
    ctx = consume(tmp_path,s3,repository_ids=(ID,42))
    assert ctx.for_repository(ID).trending_rank.value == 1
    assert ctx.for_repository(42).trending_rank.status == 'unknown'
    assert len(ctx.trending.entries) == 1


def test_trending_same_time_page_conflict(tmp_path):
    s3 = both()
    p = observation(index=2)
    seed(s3,replace(p,trending=replace(p.trending,page_sha256='c'*64)))
    ctx = consume(tmp_path,s3)
    assert ctx.trending.status == 'CONFLICT'
    assert ctx.for_repository(ID).trending_rank.status == 'unknown'
    assert ctx.for_repository(ID).comparison.star_delta.value == 200


def test_trending_unresolved_page_cannot_inject_id_from_another_object(tmp_path):
    s3 = ObservationS3()
    p = observation()
    # Name doesn't match this object's verified ID; page remains unresolved.
    p = replace(p,trending=replace(p.trending,entries=(TrendingEntry('unknown/project',1),)),boundary_coverage='PARTIAL')
    seed(s3,p)
    ctx = consume(tmp_path,s3)
    assert ctx.trending.status == 'MISSING'
    assert ctx.for_repository(ID).trending_rank.status == 'unknown'


@pytest.mark.parametrize('error,category',[(FakeClientError('AccessDenied',403),'AUTHENTICATION_OR_PERMISSION'),
    (FakeClientError('InvalidAccessKeyId',401),'AUTHENTICATION_OR_PERMISSION'),
    (OSError('sensitive secret'),'NETWORK_ERROR')])
def test_store_error_no_legacy_fallback(tmp_path,error,category):
    s3 = both(); s3.fail['list'] = error
    ctx = consume(tmp_path,s3,repository_ids=(ID,),legacy_snapshots=(legacy(),))
    assert category in {d.category for d in ctx.diagnostics}
    assert ctx.for_repository(ID).current.status == 'UNAVAILABLE'
    assert 'sensitive' not in repr(ctx)


@pytest.mark.parametrize('corrupt,category',[('json','INVALID_JSON'),('checksum','CHECKSUM_MISMATCH')])
def test_corrupt_object_invalidates_set(tmp_path,corrupt,category):
    s3 = both(); s3.corrupt = corrupt
    ctx = consume(tmp_path,s3,repository_ids=(ID,))
    assert category in {d.category for d in ctx.diagnostics}
    assert ctx.for_repository(ID).comparison.star_delta.status == 'unknown'


def test_list_truncation_not_empty(tmp_path):
    s3 = both(); s3.truncated = True
    ctx = consume(tmp_path,s3,repository_ids=(ID,))
    assert 'INCOMPLETE_LIST' in {d.category for d in ctx.diagnostics}
    assert all(c.status == 'UNAVAILABLE' for c in ctx.coverage)


def test_listed_object_disappears_not_normal_missing(tmp_path):
    s3 = both()
    original = s3.get_object
    def get(**kw):
        if '/2026-11-01/' in kw['Key']: raise FakeClientError('NoSuchKey',404)
        return original(**kw)
    s3.get_object = get
    ctx = consume(tmp_path,s3,repository_ids=(ID,))
    assert 'LISTED_OBJECT_MISSING' in {d.category for d in ctx.diagnostics}
    assert ctx.for_repository(ID).previous.status == 'UNAVAILABLE'


def test_read_budget_sixteen_gets_two_lists_and_no_writes(tmp_path):
    s3 = ObservationS3()
    for boundary in (B-timedelta(days=7),B):
        for index in range(1,9): seed(s3,observation(boundary,index=index,delay=index*60))
    original = deepcopy(s3.objects)
    ctx = consume(tmp_path,s3)
    assert s3.calls.count('list') == 2 and s3.calls.count('get') == 16
    assert s3.objects == original
    assert len(ctx.repositories) == 1
    assert s3.objects[PRODUCTION_KEY] == b'protected production bytes'


def test_nine_objects_fail_closed(tmp_path):
    s3 = ObservationS3()
    for index in range(1,10): seed(s3,observation(index=index))
    ctx = consume(tmp_path,s3,repository_ids=(ID,))
    assert 'OBJECT_LIMIT' in {d.category for d in ctx.diagnostics}
    assert ctx.for_repository(ID).current.status == 'UNAVAILABLE'


def test_cutoff_freezes_list_and_excludes_later_publication(tmp_path):
    s3 = both()
    seed(s3,observation(index=2,stars=5000),modified=CUTOFF+timedelta(seconds=1))
    ctx = consume(tmp_path,s3)
    assert ctx.for_repository(ID).current.snapshot.stars == 1200
    assert s3.calls.count('get') == 2


def test_fixture_masquerade_rejected_before_selection(tmp_path):
    s3 = both()
    for key in tuple(s3.objects):
        if key.startswith('test-observations/v1/'):
            target = key.replace('test-observations/v1/','observations/v1/production/')
            s3.objects[target] = s3.objects[key]
            s3.modified[target] = s3.modified[key]
    ctx = consume(tmp_path,s3,namespace='production',repository_ids=(ID,))
    assert 'FIXTURE_NAMESPACE_MISMATCH' in {d.category for d in ctx.diagnostics}
    assert ctx.for_repository(ID).current.status == 'UNAVAILABLE'


@pytest.mark.parametrize('period',[
    ReportPeriod(B-timedelta(days=6),B), ReportPeriod(B-timedelta(days=7),B+timedelta(seconds=1)),
    ReportPeriod(B,B+timedelta(days=7)),
])
def test_invalid_period_no_requests(tmp_path,period):
    s3 = both()
    with R2ObservationStore(settings(tmp_path/'untouched.db'),as_of=CUTOFF,namespace='fixture',profile='report_read',client=s3) as store:
        with pytest.raises(ObservationConsumerError): consume_observations(store=store,period=period,cutoff=CUTOFF)
    assert s3.calls == []


def test_same_time_metadata_conflict_not_arbitrarily_selected(tmp_path):
    s3 = both()
    p = observation(index=2)
    row = p.repositories[0]
    seed(s3,replace(p,repositories=(replace(row,metadata=replace(row.metadata,description='Different fact')),)))
    assert consume(tmp_path,s3).for_repository(ID).current.status == 'CONFLICT'


def test_trending_duplicate_page_does_not_add_signals(tmp_path):
    s3 = both()
    seed(s3,observation(index=2))
    ctx = consume(tmp_path,s3)
    assert ctx.trending.status == 'CONFIRMED'
    assert len(ctx.repositories) == 1
    assert ctx.for_repository(ID).trending_rank.value == 1
    assert not hasattr(ctx,'signals') and not hasattr(ctx,'evidence_sources')


def test_cutoff_not_refreshed_when_objects_arrive_during_read(tmp_path):
    s3 = both()
    original = s3.get_object
    def get(**kw):
        seed(s3,observation(index=2,stars=9000),modified=CUTOFF+timedelta(seconds=1))
        return original(**kw)
    s3.get_object = get
    ctx = consume(tmp_path,s3)
    assert ctx.cutoff == CUTOFF
    assert ctx.for_repository(ID).current.snapshot.stars == 1200
    assert s3.calls.count('list') == 2 and s3.calls.count('get') == 2


def test_wrong_source_url_rejected_even_with_matching_checksum(tmp_path):
    s3 = both()
    p = observation(index=2)
    seed(s3,replace(p,repositories=(replace(p.repositories[0],source_url='https://untrusted.example/'),)))
    ctx = consume(tmp_path,s3,repository_ids=(ID,))
    assert 'INVALID_SOURCE_URL' in {d.category for d in ctx.diagnostics}
    assert ctx.for_repository(ID).current.status == 'UNAVAILABLE'


def test_inflight_request_end_outside_window_is_ineligible(tmp_path):
    s3 = ObservationS3()
    seed(s3,observation(delay=3660,request_delay=3540))
    ctx = consume(tmp_path,s3,repository_ids=(ID,))
    assert ctx.for_repository(ID).current.status == 'MISSING'
    assert ctx.for_repository(ID).trending_rank.status == 'unknown'
    assert 'SOURCE_INELIGIBLE' in {d.category for d in ctx.diagnostics}


def test_input_request_generator_and_boolean_id_rejected_before_io(tmp_path):
    for ids in ((i for i in range(3)),(True,)):
        s3 = both()
        with pytest.raises(ObservationConsumerError): consume(tmp_path,s3,repository_ids=ids)
        assert s3.calls == []


def test_kst_period_representation_same_utc_boundary(tmp_path):
    from services.scheduler import KST
    s3 = both()
    with R2ObservationStore(settings(tmp_path/'untouched.db'),as_of=CUTOFF,namespace='fixture',profile='report_read',client=s3) as store:
        ctx = consume_observations(store=store,cutoff=CUTOFF,
            period=ReportPeriod(PERIOD.start.astimezone(KST),PERIOD.end.astimezone(KST)))
    assert ctx.period_end == B and ctx.period_start == PERIOD.start
    assert ctx.for_repository(ID).comparison.star_delta.value == 200


def test_existing_pipeline_runner_and_scoring_not_connected():
    import ast
    from pathlib import Path
    for name in ('services/pipeline.py','services/weekly_live_runner.py','services/score_service.py',
                 'services/validation_service.py','services/ranking_service.py'):
        tree = ast.parse(Path(name).read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                assert node.module not in {'services.observation_consumer','models.observation_context'}
            if isinstance(node,ast.Import):
                assert all(alias.name not in {'services.observation_consumer','models.observation_context'} for alias in node.names)


def test_existing_weekly_snapshot_adapter_does_not_invent_request_time(tmp_path):
    from models.domain import WeeklySnapshot
    from services.observation_consumer import legacy_from_weekly_snapshot
    snapshot = WeeklySnapshot('id:'+str(ID),B.isoformat(),(B+timedelta(seconds=60)).isoformat(),1200,80)
    original = deepcopy(snapshot)
    legacy_row = legacy_from_weekly_snapshot(snapshot,fixture=True)
    ctx = consume(tmp_path,ObservationS3(),legacy_snapshots=(legacy_row,),repository_ids=(ID,))
    assert 'LEGACY_REQUEST_TIME_MISSING' in {d.category for d in ctx.diagnostics}
    assert snapshot == original
    enriched = legacy_from_weekly_snapshot(snapshot,fixture=True,request_started_at=B+timedelta(seconds=59))
    assert consume(tmp_path,ObservationS3(),legacy_snapshots=(enriched,)).for_repository(ID).current.status == 'CONFIRMED'


def test_legacy_adapter_does_not_infer_numeric_id_from_name():
    from models.domain import WeeklySnapshot
    from services.observation_consumer import legacy_from_weekly_snapshot
    snapshot = WeeklySnapshot('name:owner/repo',B.isoformat(),B.isoformat(),100,20)
    with pytest.raises(ObservationConsumerError,match='INVALID_LEGACY_SNAPSHOT'):
        legacy_from_weekly_snapshot(snapshot)


def test_store_cutoff_advance_is_rejected_not_silently_adopted(tmp_path):
    s3 = both()
    with R2ObservationStore(settings(tmp_path/'untouched.db'),as_of=CUTOFF,namespace='fixture',profile='report_read',client=s3) as store:
        original = s3.get_object
        def get(**kw):
            response = original(**kw)
            store.advance_cutoff(CUTOFF+timedelta(seconds=1))
            return response
        s3.get_object = get
        ctx = consume_observations(store=store,period=PERIOD,cutoff=CUTOFF,repository_ids=(ID,))
    assert 'CONSUMER_CUTOFF_CHANGED' in {d.category for d in ctx.diagnostics}
    assert ctx.cutoff == CUTOFF
    assert ctx.for_repository(ID).current.status == 'UNAVAILABLE'


def test_summary_distinguishes_read_coverage_and_comparison_coverage(tmp_path):
    s3 = both()
    seed(s3,observation(index=2,stars=8000))
    ctx = consume(tmp_path,s3)
    assert all(c.status == 'COMPLETE' for c in ctx.coverage)
    assert ctx.summary.current_confirmed == 0
    assert ctx.summary.previous_confirmed == 1
    assert ctx.summary.conflicted_repositories == 1
    assert ctx.summary.star_comparisons_confirmed == 0
