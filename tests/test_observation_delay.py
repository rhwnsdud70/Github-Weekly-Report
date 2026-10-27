from datetime import datetime, timedelta

import pytest

from database.db import Database
from models.domain import Candidate, Repository, Metric, Event
from services.candidate_diagnostics import build_candidate_diagnostics, candidate_diagnostic_summary
from services.history_service import attach_history
from services.metric_service import calculate_metrics
from services.observation_timing import observation_allowed, format_observation_timing
from services.pipeline import ReportPipeline
from services.ranking_service import InsufficientCandidates, select_top
from services.scheduler import KST, report_period
from services.validation_service import validate_candidate
from services.weekly_live_runner import run_weekly_live_once
from services.r2_persistence import R2DatabaseStore
from tests.test_r2_persistence import FakeS3
from tests.test_weekly_live_once import live_settings, empty_remote, terminal_factory


BOUNDARY = datetime(2026,10,25,19,tzinfo=KST)


@pytest.mark.parametrize('delay,eligible',[(420,True),(1800,True),(3600,True),(3601,False),(14400,False),(14565,False)])
def test_actual_delay_diagnostics_do_not_move_report_boundary(delay,eligible):
    now = BOUNDARY+timedelta(seconds=delay)
    period = report_period(now)
    assert period.end == BOUNDARY and period.end-period.start == timedelta(hours=168)
    assert observation_allowed(now,period.end) is eligible
    text = format_observation_timing(period,now,now)
    assert f'Actual runner start: {now.isoformat()}' in text
    assert f'{delay:g}s after report end' in text
    for source in ('Trending','Snapshot'):
        assert f'{source} observation: '+('eligible' if eligible else 'ineligible') in text
    if not eligible:
        assert 'live Trending; current Star/Fork snapshot comparisons' in text


def test_restore_delay_diagnostics_use_current_time_not_pinned_start(tmp_path,capsys):
    configured = live_settings(tmp_path/'production.db')
    # Simulate a restore crossing the observation window. All I/O is Mock S3.
    remote = FakeS3(empty_remote(tmp_path))
    times = iter([BOUNDARY+timedelta(minutes=7),BOUNDARY+timedelta(hours=4)])
    result = run_weekly_live_once(configured,clock=lambda:next(times),
        store=R2DatabaseStore(configured,remote),pipeline_factory=terminal_factory(),
        output=tmp_path/'output')
    assert result.status == 'SUCCESS'
    text = capsys.readouterr().out
    assert 'Actual runner start: 2026-10-25T19:07:00+09:00' in text
    assert 'Collection delay: 4h 0m 0s' in text
    assert 'Snapshot observation: ineligible' in text


@pytest.mark.parametrize('previous_delay,current_delay,expected',[
    (420,420,()),(3600,3600,()),
    (420,14565,('current_observation_outside_window',)),
    (14565,420,('previous_observation_outside_window',)),
    (14565,14565,('current_observation_outside_window','previous_observation_outside_window')),
])
def test_snapshot_unusable_reasons_are_not_mislabeled_missing(previous_delay,current_delay,expected):
    period = report_period(BOUNDARY)
    db = Database(':memory:')
    item = Candidate(Repository(1,'owner/repo','https://github.com/owner/repo',stars=1200,forks=100))
    try:
        db.repository(item.repository.identity,{'full_name':'owner/repo','url':item.repository.url})
        db.snapshot(item.repository.identity,period.start.isoformat(),{
            'stars':1000,'forks':80,'measured_at':(period.start+timedelta(seconds=previous_delay)).isoformat()})
        attach_history(item,db,period,(BOUNDARY+timedelta(seconds=current_delay)).isoformat())
        assert item.previous['snapshot_unusable_reasons'] == list(expected)
        assert item.previous['snapshot_usable'] is (not expected)
        if expected:
            assert item.metric('star_delta').value is None
            assert item.metric('star_delta').source == 'snapshot observation outside boundary tolerance'
            summary = candidate_diagnostic_summary(build_candidate_diagnostics([item]))
            for reason in expected:
                assert summary['data_gaps'][reason] == 1
            assert 'previous_snapshot_missing' not in summary['data_gaps']
        else:
            assert item.metric('star_delta').value == 200
    finally:
        db.close()


def test_new_repository_previous_stars_stay_unknown():
    db = Database(':memory:')
    item = Candidate(Repository(99,'new/repo','https://github.com/new/repo',stars=1000))
    try:
        attach_history(item,db,report_period(BOUNDARY),BOUNDARY.isoformat())
        assert item.previous['snapshot_unusable_reasons'] == ['previous_snapshot_missing']
        assert item.metric('star_delta').status == item.metric('star_growth').status == 'unknown'
        assert item.metric('star_delta').source == 'previous snapshot missing'
    finally:
        db.close()


@pytest.mark.parametrize('start_delay,completion_delay',[(300,420),(3540,3660)])
def test_failed_metadata_batch_keeps_real_timestamp_and_next_week_comparison(tmp_path,monkeypatch,start_delay,completion_delay):
    from services.candidate_service import CandidateCollector
    period = report_period(BOUNDARY)
    now = [BOUNDARY+timedelta(seconds=start_delay)]
    item = Candidate(Repository(1,'owner/repo','https://github.com/owner/repo',stars=1200))
    def collect(self,*args):
        now[0] = BOUNDARY+timedelta(seconds=completion_delay)
        return [item]
    monkeypatch.setattr(CandidateCollector,'collect',collect)
    class Github:
        def begin_budget(self,*args): pass
        def end_budget(self): pass
        def activity_counts(self,*args): return {'owner/repo':{'issues':0,'pulls':0}}
        def releases(self,*args): return []
    db = Database(tmp_path/'isolated.db')
    try:
        db.repository(item.repository.identity,{'full_name':'owner/repo','url':item.repository.url})
        db.snapshot(item.repository.identity,period.start.isoformat(),{
            'stars':1000,'measured_at':period.start.isoformat()})
        db.connection.commit()
        with pytest.raises(InsufficientCandidates) as caught:
            ReportPipeline(db,Github(),object(),object(),object(),clock=lambda:now[0]).run(
                period,mode='live',output=tmp_path/'output')
        snapshot = db.previous_snapshot(item.repository.identity,period.end.isoformat())
        assert snapshot['measured_at'] == now[0].isoformat()
        assert snapshot['stars'] == 1200
        late = completion_delay > 3600
        assert caught.value.diagnostics['data_gaps'].get('current_observation_outside_window',0) == int(late)
        assert db.rows('SELECT star_delta_status FROM weekly_metrics WHERE period_end=?',
                       (period.end.isoformat(),))[0]['star_delta_status'] == ('unknown' if late else 'confirmed')
        assert db.rows('SELECT status FROM automation_runs')[0]['status'] == 'FAILED'
        assert db.rows('SELECT * FROM report_history') == db.rows('SELECT * FROM telegram_logs') == []
        next_period = report_period(BOUNDARY+timedelta(days=7,minutes=7))
        next_candidate = Candidate(Repository(1,'owner/repo','https://github.com/owner/repo',stars=1400))
        attach_history(next_candidate,db,next_period,(next_period.end+timedelta(minutes=7)).isoformat())
        assert next_candidate.metric('star_delta').value == (None if late else 200)
        assert db.previous_snapshot('id:1',period.end.isoformat())['measured_at'] == snapshot['measured_at']
    finally:
        db.close()


def test_real_independent_event_and_activity_support_top5_without_stars_or_trending():
    period = report_period(BOUNDARY)
    candidates = []
    for index in range(5):
        item = Candidate(Repository(index,f'owner/repo{index}',f'https://github.com/owner/repo{index}',stars=1000))
        item.metrics.update(issues=Metric.known(12),pulls=Metric.known(7),
                            previous_issues=Metric.known(6),previous_pulls=Metric.known(4))
        item.events.append(Event('major','v2.0.0',item.repository.url+'/releases/v2',
                                 (BOUNDARY-timedelta(days=1)).isoformat(),'release'))
        calculate_metrics(item,period)
        assert validate_candidate(item,period)
        assert item.signals == {'activity','release'}
        candidates.append(item)
    top,_ = select_top(candidates,period)
    assert len(top) == 5 and len({item.repository.identity for item in top}) == 5
    assert all(item.metric('trending').value is None and item.metric('star_delta').value is None for item in top)


def test_duplicate_discovery_and_issue_pr_counts_do_not_create_independent_evidence():
    from services.duplicate_service import merge_candidates
    item = Candidate(Repository(1,'owner/repo','https://github.com/owner/repo',stars=1000))
    item.metrics.update(issues=Metric.known(50),pulls=Metric.known(60))
    event = Event('major','v2.0.0',item.repository.url+'/releases/v2',
                  (BOUNDARY-timedelta(days=1)).isoformat(),'release')
    item.events = [event,event]
    merged = merge_candidates([item,item])[0]
    calculate_metrics(merged,report_period(BOUNDARY))
    assert validate_candidate(merged,report_period(BOUNDARY))
    assert merged.signals == {'activity','release'}
    merged.events = []
    assert not validate_candidate(merged,report_period(BOUNDARY))
    assert merged.signals == {'activity'}  # Two APIs are not two independent signals.
