import json
import logging
from services.cli import main, RedactSecrets


def test_cli_dry_structure_and_init_without_credentials(tmp_path,monkeypatch,capsys):
    monkeypatch.chdir(tmp_path)
    env = tmp_path/'config.env'
    env.write_text('DATABASE_PATH=production.db\n')
    assert main(['--env',str(env),'init-db']) == 0
    assert (tmp_path/'production.db').is_file()
    original = (tmp_path/'production.db').read_bytes()
    output = tmp_path/'dry'
    assert main(['--env',str(env),'run','--dry-run','--candidate-limit','10','--structure-only',
                 '--output',str(output),'--at','2026-10-04T19:00:00+09:00']) == 0
    result = json.loads((output/'verification.json').read_text(encoding='utf-8'))
    assert result['llm_call_count'] == 0
    assert result['candidate_count'] == 10
    assert (tmp_path/'production.db').read_bytes() == original


def test_log_secret_redaction():
    record = logging.LogRecord('test',logging.INFO,'test',1,'token=%s',('private-value',),None)
    assert RedactSecrets(['private-value']).filter(record)
    assert record.getMessage() == 'token=[REDACTED]'
