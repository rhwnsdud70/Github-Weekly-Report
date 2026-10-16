from datetime import datetime, timedelta
import json

import pytest

from database.db import Database
from models.domain import Candidate, Repository, Metric
from services.candidate_diagnostics import (
    build_candidate_diagnostics, candidate_diagnostic_summary, format_candidate_diagnostics,
)
from services.history_service import attach_history
from services.metric_service import calculate_metrics
from services.pipeline import ReportPipeline
from services.ranking_service import InsufficientCandidates
from services.scheduler import KST, report_period
from tests.test_candidate_diagnostics import candidate


BOUNDARY = datetime(2026,10,11,19,tzinfo=KST)


@pytest.mark.parametrize('delay,expected',[(0,True),(300,True),(3600,True),(3601,False),(5*86400,False)])
def test_actual_live_trending_gate_and_failed_run_summary(tmp_path,monkeypatch,delay,expected):
    from services.candidate_service import CandidateCollector
    flags = []
    def collect(self,period,limit,include_trending,names):
        flags.append(include_trending)
        return []
    monkeypatch.setattr(CandidateCollector,'collect',collect)
    class Github:
        def begin_budget(self,*args): pass
        def end_budget(self): pass
        def activity_counts(self,*args): return {}
    now = BOUNDARY+timedelta(seconds=delay)
    period = report_period(now)
    assert period.end == BOUNDARY
    assert period.end-period.start == timedelta(hours=168)
    db = Database(tmp_path/'isolated.db')
    try:
        pipeline = ReportPipeline(db,Github(),object(),object(),object(),clock=lambda:now)
        with pytest.raises(InsufficientCandidates) as caught:
            pipeline.run(period,mode='live',output=tmp_path/'output')
        assert flags == [expected]
        summary = candidate_diagnostic_summary(caught.value.diagnostics)
        assert summary['historical_trending_unavailable'] is (not expected)
        assert summary['kind'] == 'NO_DISCOVERY_CANDIDATES'
        assert summary['funnel']['discovered'] == summary['funnel']['selectable'] == 0
        saved = db.rows('SELECT status,report FROM automation_runs')[0]
        assert saved['status'] == 'FAILED'
        assert json.loads(saved['report'])['candidate_diagnostics'] == summary
        assert period.contains(period.start) and not period.contains(period.end)
    finally:
        db.close()


def test_before_sunday_boundary_uses_last_completed_week():
    period = report_period(BOUNDARY-timedelta(seconds=1))
    assert period.end == BOUNDARY-timedelta(days=7)
    assert period.end-period.start == timedelta(hours=168)


def test_cold_start_unknown_stars_forks_but_api_previous_activity_can_be_known():
    db = Database(':memory:')
    period = report_period(BOUNDARY)
    item = Candidate(Repository(1,'owner/repo','https://github.com/owner/repo',stars=1000,forks=100))
    try:
        attach_history(item,db,period,BOUNDARY.isoformat())
        for key,value in {'issues':20,'pulls':10,'previous_issues':10,'previous_pulls':5}.items():
            item.metrics[key] = Metric.known(value,'mock exact GitHub activity')
        calculate_metrics(item,period)
        for key in ('star_delta','star_growth','fork_delta','fork_growth','discussion_delta'):
            assert item.metric(key).status == 'unknown'
            assert item.metric(key).value is None
        assert item.metric('activity_growth').value == 100
        diagnostic = build_candidate_diagnostics([item])
        assert diagnostic['data_gaps']['previous_snapshot_missing'] == 1
        assert diagnostic['data_gaps']['star_growth_unknown'] == 1
    finally:
        db.close()


@pytest.mark.parametrize('offset,usable',[(0,True),(300,True),(3600,True),(3601,False),(5*86400,False)])
def test_previous_snapshot_must_have_been_measured_near_previous_boundary(offset,usable):
    db = Database(':memory:')
    period = report_period(BOUNDARY)
    item = Candidate(Repository(1,'owner/repo','https://github.com/owner/repo',stars=1200))
    try:
        db.repository(item.repository.identity,{'github_id':1,'full_name':'owner/repo','url':item.repository.url})
        db.snapshot(item.repository.identity,period.start.isoformat(),{
            'measured_at':(period.start+timedelta(seconds=offset)).isoformat(),'stars':1000})
        attach_history(item,db,period,BOUNDARY.isoformat())
        assert item.previous['snapshot_usable'] is usable
        assert item.metric('star_delta').value == (200 if usable else None)
        assert item.metric('star_growth').value == (20 if usable else None)
        diagnostic = build_candidate_diagnostics([item])
        assert diagnostic['data_gaps'].get('previous_snapshot_unusable',0) == int(not usable)
    finally:
        db.close()


def test_summary_allowlist_does_not_echo_exception_or_source_text():
    diagnostic = build_candidate_diagnostics([candidate(1)],historical_trending_unavailable=True)
    diagnostic['secret'] = 'secret-value'
    diagnostic['funnel']['secret-value'] = 1
    diagnostic['exclusion_reasons']['secret-value'] = 1
    diagnostic['data_gaps']['secret-value'] = 1
    diagnostic['top_candidates'] = [{'repository':'secret-value'}]
    summary = candidate_diagnostic_summary(diagnostic)
    assert 'secret-value' not in json.dumps(summary)
    assert 'secret-value' not in format_candidate_diagnostics(summary)
    assert summary['historical_trending_unavailable']


@pytest.mark.parametrize('value',[None,{},'secret-value',{'kind':'secret-value'},
    {'kind':'INSUFFICIENT_EVIDENCE','funnel':{'discovered':'secret-value'}}])
def test_missing_or_malformed_diagnostics_are_not_guessed(value):
    assert candidate_diagnostic_summary(value) is None


def test_diagnostics_use_actual_star_growth_metric_name():
    item = candidate(1)
    diagnostic = build_candidate_diagnostics([item])
    assert 'star_growth:confirmed' in diagnostic['top_candidates'][0]['metrics_available']
