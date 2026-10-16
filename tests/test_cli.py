import json
import logging
import pytest
from services.cli import main, RedactSecrets


@pytest.mark.parametrize('inherited_path',[None,'data/inherited-production.db'])
def test_cli_dry_structure_and_init_without_credentials(tmp_path,monkeypatch,capsys,inherited_path):
    monkeypatch.chdir(tmp_path)
    if inherited_path is not None:
        monkeypatch.setenv('DATABASE_PATH',inherited_path)
    # This test exercises the file configuration, not OS-over-file precedence.
    monkeypatch.delenv('DATABASE_PATH',raising=False)
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


@pytest.mark.parametrize('inherited_path',[None,'ambient.db'])
def test_cli_env_file_and_os_environment_precedence(tmp_path,monkeypatch,inherited_path):
    monkeypatch.chdir(tmp_path)
    if inherited_path is None:
        monkeypatch.delenv('DATABASE_PATH',raising=False)
    else:
        monkeypatch.setenv('DATABASE_PATH',inherited_path)
    env = tmp_path/'config.env'
    env.write_text('DATABASE_PATH=from-file.db\n')
    assert main(['--env',str(env),'init-db']) == 0
    expected = inherited_path or 'from-file.db'
    assert (tmp_path/expected).is_file()
    if inherited_path is not None:
        assert not (tmp_path/'from-file.db').exists()


def test_log_secret_redaction():
    record = logging.LogRecord('test',logging.INFO,'test',1,'token=%s',('private-value',),None)
    assert RedactSecrets(['private-value']).filter(record)
    assert record.getMessage() == 'token=[REDACTED]'
