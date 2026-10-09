"""Inspect an existing offline run without repeating collection or generation.

Usage: python -m scripts.verify_run artifacts/full-dry-run
"""
import argparse
import json
from pathlib import Path
import sqlite3

from models.serialization import report_from_dict, dumps
from telegram.formatter import format_report
from telegram.splitter import split_messages, units


def verify(directory):
    directory = Path(directory)
    result = json.loads((directory/'verification.json').read_text(encoding='utf-8'))
    report = report_from_dict(json.loads((directory/'report.json').read_text(encoding='utf-8')))
    messages = split_messages(format_report(report))
    with sqlite3.connect((directory/'dry-run.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as db:
        counts = {table:db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in (
            'repositories','weekly_snapshots','weekly_metrics','report_history','watchlist','telegram_logs','automation_runs')}
        assert counts['automation_runs'] == 1, 'Full validation must run once in its own output directory'
        assert counts['repositories'] == result['candidate_count']
        assert counts['weekly_metrics'] == result['candidate_count']
        assert counts['report_history'] == 5
        assert counts['watchlist'] <= 3
        assert counts['telegram_logs'] == len(messages)
    assert len(report.top) == len({c.repository.identity for c in report.top}) == 5
    assert any(c.critical for c in report.top), 'Fixture Critical repository must be selected'
    assert result['deep_repository_count'] == 5 and result['llm_call_count'] == 10
    assert result['status'] == 'SUCCESS' and result['fixture_only']
    assert (report.period.end-report.period.start).total_seconds() == 168*3600
    assert all(units(message.text)<=4096 for message in messages)
    check = {'status':'PASS','source_run_id':result['run_id'],'candidate_count':result['candidate_count'],
             'top_count':len(report.top),'message_count':len(messages),
             'max_message_utf16_units':max(units(message.text) for message in messages),'table_counts':counts}
    (directory/'verification_checks.json').write_text(dumps(check),encoding='utf-8')
    return check


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    print(dumps(verify(parser.parse_args().directory)))
