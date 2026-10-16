from datetime import timedelta
import json

import pytest

from database.db import Database, DuplicateRunError
from scripts.integration_test_fixture import ColdStartIntegrationFixture
from scripts.run_full_top5_live import integration_database_path
from services.demo import DemoBackend
from services.pipeline import ReportPipeline
from telegram.splitter import units
from tests.test_ranking import period


def test_integration_database_is_separate_from_production(tmp_path, monkeypatch):
    import scripts.run_full_top5_live as runner
    production = tmp_path/'production.db'
    integration = tmp_path/'integration.db'
    monkeypatch.setattr(runner,'INTEGRATION_DATABASE',integration)
    settings = type('Settings',(),{'database_path':str(production)})()
    assert integration_database_path(settings) == integration
    assert not production.exists()
    settings.database_path = str(integration)
    with pytest.raises(ValueError,match='separate from production'):
        integration_database_path(settings)


def test_isolated_integration_mode_runs_full_flow_and_blocks_duplicate(tmp_path, monkeypatch):
    test_period = period()
    production_path = tmp_path/'production.db'
    production = Database(production_path)
    production.start_run('unchanged-start','unchanged-end','live')
    production.close()
    production_before = production_path.read_bytes()
    database = Database(tmp_path/'integration.db')
    backend = DemoBackend(test_period,10)
    client,github,llm,sender = backend.clients()
    calls = []
    import services.pipeline as pipeline_module
    production_score = pipeline_module.score_candidate
    def observed_score(candidate):
        calls.append(candidate.repository.identity)
        return production_score(candidate)
    monkeypatch.setattr(pipeline_module,'score_candidate',observed_score)
    pipeline = ReportPipeline(database,github,client,llm,sender,
                              clock=lambda:test_period.end+timedelta(days=2))
    fixture = ColdStartIntegrationFixture()

    result = pipeline.run(test_period,mode='integration-test',limit=10,output=tmp_path/'output',
                          integration_fixture=fixture)

    assert result['status'] == 'SUCCESS'
    assert len(result['top']) == 5
    assert len(calls) == result['candidate_count']
    assert backend.calls['api.openai.com/v1/responses'] == 10
    assert backend.deep_repositories == set(result['top'])
    assert backend.sent and all(item['text'].startswith('[INTEGRATION TEST]') for item in backend.sent)
    report = json.loads((tmp_path/'output'/'report.json').read_text(encoding='utf-8'))
    assert 'Historical Trending: synthetic fixture for integration testing' in report['reliability']
    assert not any('Historical Trending: unavailable' in line for line in report['reliability'])
    assert not backend.calls['github.com/trending']
    for item in backend.sent:
        if '이번 주 주요 지표' in item['text'] and 'integration fixture 기반입니다' in item['text']:
            assert '현재 Star:' in item['text']
    assert all(units(item['text']) <= 4096 for item in backend.sent)
    assert result['integration_fixture']['seeded_candidate_count'] >= 5
    assert result['integration_fixture']['fixture_only_fields'] == [
        'previous_snapshot','previous_activity','historical_trending']

    assert database.rows('SELECT mode,status FROM automation_runs') == [
        {'mode':'integration-test','status':'SUCCESS'}]
    assert len(database.rows('SELECT * FROM repositories')) >= result['candidate_count']
    assert len(database.rows('SELECT * FROM weekly_snapshots')) >= result['candidate_count']
    assert len(database.rows('SELECT * FROM weekly_metrics')) == result['candidate_count']
    assert len(database.rows('SELECT * FROM report_history')) == 5
    assert len(database.rows('SELECT * FROM watchlist')) == 3
    assert database.rows('SELECT * FROM telegram_logs')

    seeded = database.rows('SELECT data FROM weekly_snapshots WHERE period_end=?',
                           (test_period.start.isoformat(),))
    assert seeded and all('integration-test previous snapshot' in row['data'] for row in seeded)
    saved = database.rows('SELECT metrics FROM weekly_metrics')
    assert any('INTEGRATION TEST FIXTURE historical Trending' in row['metrics'] for row in saved)
    assert any(json.loads(row['metrics'])['star_delta']['status'] == 'confirmed' for row in saved)
    assert production_path.read_bytes() == production_before

    before = dict(backend.calls)
    with pytest.raises(DuplicateRunError):
        pipeline.run(test_period,mode='integration-test',limit=10,output=tmp_path/'output',
                     integration_fixture=ColdStartIntegrationFixture())
    assert dict(backend.calls) == before
    client.close()
    database.close()


def test_fixture_preserves_current_github_state_and_documents():
    from copy import deepcopy
    from models.domain import Event, Metric
    from services.history_service import attach_history
    from tests.test_ranking import sample_candidates
    candidates = sample_candidates(6)
    database = Database(':memory:')
    fixture = ColdStartIntegrationFixture()
    current = period()
    for candidate in candidates:
        candidate.repository.description = 'Actual repository description'
        candidate.repository.forks = 200
        candidate.metrics.update(issues=Metric.known(12,'GitHub'),pulls=Metric.known(7,'GitHub'),
                                 releases_checked=Metric.known(True,'GitHub'))
        candidate.events = [Event('major','Actual release','https://github.com/release',
                                 current.start.isoformat(),'release',{'body':'Actual notes'})]
        candidate.evidence = {'documents':[{'source_type':'PR','text':'Actual PR body'}]}
    before = deepcopy(candidates)
    fixture.seed_history(database,candidates,current)
    for candidate in candidates:
        attach_history(candidate,database,current,fixture.observed_at(current))
    fixture.apply_metrics(candidates,current)
    for old,new in zip(before,candidates):
        assert new.repository == old.repository
        assert new.events == old.events
        assert new.evidence == old.evidence
        for name in ('issues','pulls','releases_checked'):
            assert new.metric(name) == old.metric(name)
        assert new.metric('star_delta').value > 0
        assert new.metric('star_growth').value > 0
        assert new.metric('trending').value is True
        assert new.metric('trending_rank').source.startswith('INTEGRATION TEST FIXTURE')
        assert new.metric('issues_growth').source.startswith('INTEGRATION TEST FIXTURE')
        assert new.previous['snapshot']['stars'] < new.repository.stars
    database.close()


def test_fixture_cannot_enter_production_live_mode(tmp_path):
    test_period = period()
    database = Database(tmp_path/'production.db')
    backend = DemoBackend(test_period,10)
    client,github,llm,sender = backend.clients()
    pipeline = ReportPipeline(database,github,client,llm,sender,clock=lambda:test_period.end)
    with pytest.raises(ValueError,match='required only for integration-test'):
        pipeline.run(test_period,mode='live',limit=10,output=tmp_path/'output',
                     integration_fixture=ColdStartIntegrationFixture())
    assert database.rows('SELECT * FROM automation_runs') == []
    assert not backend.calls
    client.close()
    database.close()


@pytest.mark.parametrize('late',[False,True])
def test_normal_live_trending_context_never_uses_integration_marker(tmp_path,late):
    current = period()
    backend = DemoBackend(current,10)
    client,github,llm,sender = backend.clients()
    database = Database(':memory:')
    backend.seed_history(database)
    pipeline = ReportPipeline(database,github,client,llm,sender,
                              clock=lambda:current.end+timedelta(days=2 if late else 0))
    pipeline.run(current,mode='live',limit=10,output=tmp_path)
    report = json.loads((tmp_path/'report.json').read_text(encoding='utf-8'))
    unavailable = 'Historical Trending: unavailable (expected for retrospective manual run)'
    assert (unavailable in report['reliability']) == late
    assert bool(backend.calls['github.com/trending']) == (not late)
    text = '\n'.join(item['text'] for item in backend.sent)
    assert '[INTEGRATION TEST]' not in text
    assert 'integration fixture 기반입니다' not in text
    assert 'synthetic fixture for integration testing' not in text
    client.close()
    database.close()
