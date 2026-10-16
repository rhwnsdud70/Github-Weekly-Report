import json
from pathlib import Path
import httpx
from config.settings import Settings
from scripts.send_telegram_top1_live import build_messages, run


def artifact(tmp_path):
    root = tmp_path/'artifact'
    root.mkdir()
    (root/'run_metadata.json').write_text(json.dumps({
        'validation_status':'SUCCESS','repository':'damianvtran/local-operator',
        'period_start':'2026-01-01T00:00:00+09:00','period_end':'2026-01-08T00:00:00+09:00'}),encoding='utf-8')
    (root/'source_evidence.json').write_text(json.dumps({
        'repository':{'github_id':1,'full_name':'damianvtran/local-operator','url':'repo'},
        'important_links':[{'kind':'issues','url':'issue'}],
        'notable_events':[{'source_type':'Release','text':'v1','url':'release',
                           'occurred_at':'2026-01-02T00:00:00+09:00'}]}),encoding='utf-8')
    (root/'final_narrative.json').write_text(json.dumps({
        'background':[],'trigger':[],'attention':['확인된 관심'], 'reaction':[],
        'project':['프로젝트 설명'],'impact':[],'positive':[],'concerns':[],
        'debates':[],'risks':[]}),encoding='utf-8')
    return root


def test_artifact_formatter_omits_empty_sections_and_marks_test(tmp_path):
    messages,_ = build_messages(artifact(tmp_path))
    text = '\n'.join(message.text for message in messages)
    assert '[TEST] GitHub Weekly Report - TOP 1' in text
    assert 'damianvtran/local-operator' in text
    assert '개발자에게 왜 중요한가' not in text
    assert '확인 가능한 근거 없음' not in text


def test_telegram_test_uses_separate_mode_and_logs(tmp_path):
    root = artifact(tmp_path)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'ok':True})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    db_path = tmp_path/'telegram-test.db'
    messages,results,status,_ = run(Settings(telegram_bot_token='fake',telegram_chat_id='chat'),
        root,db_path,client=client,sleep=lambda _:None)
    import sqlite3
    connection = sqlite3.connect(db_path)
    assert connection.execute('SELECT mode FROM automation_runs').fetchone()[0] == 'telegram-test'
    assert connection.execute('SELECT message_type FROM telegram_logs').fetchone()[0] == 'telegram-test'
    assert status == 'SUCCESS' and results[0].http_status == 200
    assert len(messages) == len(calls) == 1
