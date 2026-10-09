import json
import pytest
from database.db import Database, DuplicateRunError
from models.serialization import report_from_dict
from services.demo import DemoBackend
from services.pipeline import ReportPipeline, write_output
from telegram.splitter import units
from tests.test_ranking import period


def test_limited_integration_reuses_discovery_and_cached_report(tmp_path):
    db = Database(tmp_path/'dry.db')
    backend = DemoBackend(period(),10)
    backend.seed_history(db)
    client,github,llm,sender = backend.clients()
    pipeline = ReportPipeline(db,github,client,llm,sender,clock=lambda:period().end)
    selection = pipeline.run(period(),limit=10,output=tmp_path,structure_only=True)
    assert selection['candidate_count'] == 10
    assert not backend.deep_repositories
    assert backend.calls['api.openai.com/v1/responses'] == 0
    searches = backend.calls['api.github.com/search/repositories']
    result = pipeline.run(period(),limit=10,output=tmp_path,candidate_cache=tmp_path/'candidates.json')
    assert result['status'] == 'SUCCESS'
    assert len(result['top']) == 5
    assert backend.repositories[-1]['full_name'] in result['top']
    assert backend.calls['api.github.com/search/repositories'] == searches
    assert backend.deep_repositories == set(result['top'])
    assert backend.calls['api.openai.com/v1/responses'] == 10
    assert len(db.rows('SELECT * FROM report_history')) == 5
    assert len(db.rows('SELECT * FROM watchlist')) == 3
    assert len(db.rows('SELECT * FROM weekly_metrics')) == 10
    assert all(units(item['text'])<=4096 for item in backend.sent)
    assert all(item['link_preview_options']['is_disabled'] for item in backend.sent)
    calls = dict(backend.calls)
    report = report_from_dict(json.loads((tmp_path/'report.json').read_text(encoding='utf-8')))
    write_output(report,tmp_path/'rendered')
    assert dict(backend.calls) == calls
    assert (tmp_path/'messages.txt').read_bytes() == (tmp_path/'rendered'/'messages.txt').read_bytes()
    client.close()
    db.close()


def test_mock_live_partial_failure_no_resend(tmp_path):
    db = Database(tmp_path/'live.db')
    backend = DemoBackend(period(),10,failed_message='TOP 2')
    backend.seed_history(db)
    client,github,llm,sender = backend.clients()
    pipeline = ReportPipeline(db,github,client,llm,sender,clock=lambda:period().end)
    result = pipeline.run(period(),mode='live',limit=10,output=tmp_path)
    assert result['status'] == 'PARTIAL_SUCCESS'
    assert sum('TOP 2' in item['text'] for item in backend.sent) == 3
    assert any('TOP 5' in item['text'] for item in backend.sent)
    assert len(db.rows('SELECT * FROM report_history')) == 5
    calls = dict(backend.calls)
    with pytest.raises(DuplicateRunError):
        pipeline.run(period(),mode='live',limit=10,output=tmp_path)
    assert dict(backend.calls) == calls
    assert len(db.rows('SELECT * FROM automation_runs')) == 1
    client.close()
    db.close()


def test_insufficient_evidence_saves_observations_without_fabricated_report(tmp_path):
    import httpx
    from services.ranking_service import InsufficientCandidates
    db = Database(tmp_path/'cold.db')
    backend = DemoBackend(period(),10)
    original = backend.respond
    def respond(request):
        if request.url.path == '/search/issues':
            return httpx.Response(200,json={'items':[],'total_count':0,'incomplete_results':False})
        if request.url.path.endswith('/releases'):
            return httpx.Response(200,json=[])
        return original(request)
    backend.respond = respond
    client,github,llm,sender = backend.clients()
    pipeline = ReportPipeline(db,github,client,llm,sender,clock=lambda:period().end)
    with pytest.raises(InsufficientCandidates):
        pipeline.run(period(),limit=10,output=tmp_path)
    assert db.rows('SELECT status FROM automation_runs')[0]['status']=='FAILED'
    assert len(db.rows('SELECT * FROM weekly_snapshots')) == 10
    assert db.rows('SELECT * FROM report_history') == []
    assert not backend.sent
    assert backend.calls['api.openai.com/v1/responses'] == 0
    assert all(row['star_delta_status']=='unknown' for row in db.rows('SELECT star_delta_status FROM weekly_metrics'))
    client.close()
    db.close()
