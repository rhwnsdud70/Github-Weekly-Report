"""One manual real TOP 5 integration run. Does not start or register a scheduler."""
import argparse
from contextlib import ExitStack
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys
import httpx

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.github_client import GitHubClient
from config import rules
from config.settings import Settings
from database.db import Database, DuplicateRunError
from models.domain import ReportPeriod, instant
from services.candidate_service import CandidateCollector
from services.narrative_service import OpenAIJSONClient
from services.pipeline import ReportPipeline
from services.candidate_diagnostics import format_candidate_diagnostics
from services.ranking_service import InsufficientCandidates
from scripts.integration_test_fixture import ColdStartIntegrationFixture
from services.scheduler import KST, report_period
from telegram.sender import TelegramSender
from telegram.splitter import units


OUTPUT = Path('artifacts/full-top5-live')
INTEGRATION_OUTPUT = Path('artifacts/full-integration-test')
INTEGRATION_DATABASE = Path('data/full_integration_test.db')


def manual_period(period_end=None, now=None):
    """Use the scheduler period by default; an override is manual-run only."""
    if period_end is None:
        return report_period(now)
    end = instant(period_end)
    return ReportPeriod(end-timedelta(days=7),end)


def existing_claim(database, period, mode='live'):
    return bool(database.rows(
        'SELECT 1 FROM automation_runs WHERE julianday(period_end)=julianday(?) AND mode=?',
        (period.end.isoformat(),mode)))


def existing_live_claim(database, period):
    return existing_claim(database,period,'live')


def integration_database_path(settings):
    path = INTEGRATION_DATABASE
    if path.resolve() == Path(settings.database_path).resolve():
        raise ValueError('Integration-test database must be separate from production')
    return path


class ReportingSender(TelegramSender):
    def send(self, messages):
        print(f'Telegram messages before send: {len(messages)}', flush=True)
        for index,message in enumerate(messages,1):
            headings = [section.splitlines()[0] for section in message.sections]
            print(f'  message={index} chars={len(message.text)} utf16_units={units(message.text)} '
                  f'kind={message.kind} top_rank={message.rank} part={message.part} sections={headings}',flush=True)
        return super().send(messages)


def table_counts(database, period_end):
    return {
        'runs':len(database.rows('SELECT * FROM automation_runs WHERE period_end=?',(period_end,))),
        'snapshots':len(database.rows('SELECT * FROM weekly_snapshots WHERE period_end=?',(period_end,))),
        'metrics':len(database.rows('SELECT * FROM weekly_metrics WHERE period_end=?',(period_end,))),
        'history':len(database.rows('SELECT * FROM report_history WHERE period_end=?',(period_end,))),
        'watchlist':len(database.rows('SELECT * FROM watchlist WHERE period_end=?',(period_end,))),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--period-end',
        help='Timezone-aware manual period end; start is exactly seven days earlier')
    parser.add_argument('--integration-test',action='store_true',
        help='Use isolated DB and explicit cold-start fixtures for one manual end-to-end test')
    args = parser.parse_args(argv)
    settings = Settings.load('.env')
    settings.validate_live(send=True)
    period = manual_period(args.period_end)
    mode = 'integration-test' if args.integration_test else 'live'
    output = INTEGRATION_OUTPUT if args.integration_test else OUTPUT
    database_path = integration_database_path(settings) if args.integration_test else Path(settings.database_path)
    output.mkdir(parents=True,exist_ok=True)
    print(f'Period start: {period.start.isoformat()}',flush=True)
    print(f'Period end: {period.end.isoformat()}',flush=True)
    print('Mode: integration-test' if args.integration_test else 'Mode: manual-live',flush=True)
    if args.integration_test:
        print('Database: isolated integration-test database',flush=True)
        print(ColdStartIntegrationFixture.disclosure,flush=True)
    with ExitStack() as stack:
        database = Database(database_path)
        stack.callback(database.close)
        claimed = existing_claim(database,period,mode)
        print('Existing claim: '+('YES' if claimed else 'NO'),flush=True)
        if claimed:
            print('Duplicate-send protection: BLOCKED before collection/send')
            return 3
        client = stack.enter_context(httpx.Client(timeout=30))
        llm_client = stack.enter_context(httpx.Client(timeout=90))
        github = GitHubClient(settings.github_token,client)
        llm = OpenAIJSONClient(settings.openai_api_key,settings.openai_model,llm_client)
        sender = ReportingSender(settings.telegram_bot_token,settings.telegram_chat_id,client)
        pipeline = ReportPipeline(database,github,client,llm,sender)
        fixture = ColdStartIntegrationFixture() if args.integration_test else None
        try:
            result = pipeline.run(period,mode,rules.CANDIDATE_LIMIT,output,
                                  integration_fixture=fixture)
        except DuplicateRunError:
            print('Duplicate-send protection: BLOCKED before collection/send')
            return 3
        except InsufficientCandidates as error:
            diagnostic = getattr(error,'diagnostics',None)
            if diagnostic:
                print(format_candidate_diagnostics(diagnostic),flush=True)
            print('Full TOP5 integration: FAILED (InsufficientCandidates); secrets omitted')
            return 1
        except Exception as error:
            print(f'Full TOP5 integration: FAILED ({type(error).__name__}); secrets omitted')
            return 1

        candidates = json.loads((output/'candidates.json').read_text(encoding='utf-8'))['candidates']
        selected = set(result['top'])
        print(f'Candidate count: {result["candidate_count"]}')
        print(f'Final TOP count: {len(result["top"])}')
        for candidate in candidates:
            score = candidate.get('score') or {}
            total = sum(value for value in score.get('components',{}).values() if value is not None)
            grade = next(label for lower,label in rules.GRADE_BUCKETS if total >= lower)
            reasons = candidate.get('selection_reasons') if candidate['repository']['full_name'] in selected \
                else candidate.get('validation_reasons') or ['lower ranked than selected candidates']
            print(f'  {candidate["repository"]["full_name"]}: score={total}, grade={grade}, '
                  f'selected={candidate["repository"]["full_name"] in selected}, reasons={reasons}')
        total_usage = {'input_tokens':0,'output_tokens':0,'total_tokens':0}
        print('OpenAI usage by TOP:')
        for repository,usage in result.get('llm_usage',{}).items():
            print(f'  {repository}: input={usage.get("input_tokens",0)}, '
                  f'output={usage.get("output_tokens",0)}, total={usage.get("total_tokens",0)}')
            for key in total_usage:
                total_usage[key] += usage.get(key,0)
        print('OpenAI usage total: '+json.dumps(total_usage))
        retries = sum(max(0,item['attempts']-1) for item in result['transmissions'])
        success = sum(item['status'] in {'SUCCESS','RETRY_SUCCESS'} for item in result['transmissions'])
        failed = len(result['transmissions'])-success
        print(f'Telegram result: success={success}, failed={failed}, retries={retries}, status={result["status"]}')
        try:
            database.claim_run(period.start.isoformat(),period.end.isoformat(),mode)
            duplicate = 'FAILED (unexpected second claim)'
        except DuplicateRunError:
            duplicate = 'BLOCKED'
        print('Duplicate-send protection: '+duplicate)
        counts = table_counts(database,period.end.isoformat())
        logs = database.rows('SELECT COUNT(*) count FROM telegram_logs WHERE run_id=?',(result['run_id'],))[0]['count']
        counts['telegram_logs'] = logs
        print('Persistence: '+json.dumps(counts))
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['status']=='SUCCESS' and duplicate=='BLOCKED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
