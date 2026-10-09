import argparse
from contextlib import ExitStack
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
import sys

import httpx

from collectors.github_client import GitHubClient
from config import rules
from config.settings import Settings
from database.db import Database, DuplicateRunError
from models.domain import instant
from models.serialization import dumps, report_from_dict
from services.demo import DemoBackend
from services.narrative_service import OpenAIJSONClient
from services.pipeline import ReportPipeline, write_output
from services.scheduler import KST, report_period, scheduler_loop
from telegram.sender import TelegramSender


class RedactSecrets(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = [value for value in secrets if value]

    def filter(self, record):
        text = record.getMessage()
        for secret in self.secrets:
            text = text.replace(secret,'[REDACTED]')
        record.msg,record.args = text,()
        return True


def configure_logging(settings):
    Path('logs').mkdir(exist_ok=True)
    handler = logging.FileHandler('logs/weekly_report.log',encoding='utf-8')
    handler.addFilter(RedactSecrets([settings.github_token,settings.openai_api_key,
        settings.telegram_bot_token,settings.telegram_chat_id]))
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # httpx INFO logs request URLs, which contain the Telegram bot credential.
    for name in ('httpx','httpcore'):
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def parser():
    root = argparse.ArgumentParser(description='GitHub weekly AI/developer report')
    root.add_argument('--env',default='.env',help='Configuration .env file')
    commands = root.add_subparsers(dest='command',required=True)
    commands.add_parser('init-db',help='Initialize configured SQLite database')
    run = commands.add_parser('run',help='Run one completed weekly period')
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run',action='store_true',help='Offline synthetic fixtures; no real API calls')
    mode.add_argument('--preview',action='store_true',help='Real GitHub/OpenAI; local output; no Telegram')
    mode.add_argument('--live',action='store_true',help='Real collection, LLM and Telegram delivery')
    run.add_argument('--candidate-limit',type=int,default=rules.CANDIDATE_LIMIT)
    run.add_argument('--output',default='artifacts/report')
    run.add_argument('--at',help='Timezone-aware fixture clock; dry-run only')
    run.add_argument('--structure-only',action='store_true',help='Dry-run selection only, no deep analysis/LLM')
    run.add_argument('--resume-candidates',help='Reuse dry-run candidates.json for narrative test')
    render = commands.add_parser('format',help='Render a saved report without any API calls')
    render.add_argument('report')
    render.add_argument('--output',default='artifacts/rendered')
    schedule = commands.add_parser('schedule',help='Foreground Sunday 19:00 KST scheduler')
    schedule.add_argument('--output',default='artifacts/scheduled')
    return root


def execute_run(settings, period, mode, limit, output, structure_only=False, candidate_cache=None):
    directory = Path(output)
    directory.mkdir(parents=True,exist_ok=True)
    db_path = Path(settings.database_path) if mode == 'live' else directory/(mode+'.sqlite')
    if mode != 'live' and db_path.resolve() == Path(settings.database_path).resolve():
        raise ValueError('Offline/preview database must be separate from production')
    if mode != 'dry-run':
        settings.validate_live(send=mode=='live')
    with ExitStack() as stack:
        database = Database(db_path)
        stack.callback(database.close)
        if mode == 'preview' and Path(settings.database_path).is_file():
            # Read-only backup allows realistic comparison without altering production history.
            with sqlite3.connect(Path(settings.database_path).resolve().as_uri()+'?mode=ro',uri=True) as source:
                source.backup(database.connection)
        if mode == 'dry-run':
            backend = DemoBackend(period,limit)
            backend.seed_history(database)
            client,github,llm,sender = backend.clients()
            stack.callback(client.close)
            pipeline = ReportPipeline(database,github,client,llm,sender,clock=lambda:period.end)
        else:
            client = stack.enter_context(httpx.Client(timeout=30))
            llm_client = stack.enter_context(httpx.Client(timeout=90))
            github = GitHubClient(settings.github_token,client)
            llm = OpenAIJSONClient(settings.openai_api_key,settings.openai_model,llm_client)
            sender = TelegramSender(settings.telegram_bot_token,settings.telegram_chat_id,client) if mode=='live' else None
            pipeline = ReportPipeline(database,github,client,llm,sender)
        result = pipeline.run(period,mode,limit,output,structure_only,candidate_cache)
        if mode == 'dry-run':
            result['fixture_only'] = True
            result['llm_call_count'] = backend.calls['api.openai.com/v1/responses']
            result['deep_repository_count'] = len(backend.deep_repositories)
            (directory/'verification.json').write_text(dumps(result),encoding='utf-8')
        return result


def main(argv=None):
    args = parser().parse_args(argv)
    handler = None
    try:
        settings = Settings.load(args.env)
        handler = configure_logging(settings)
        if args.command == 'init-db':
            Database(settings.database_path).close()
            print('Database initialized: '+settings.database_path)
        elif args.command == 'format':
            report = report_from_dict(json.loads(Path(args.report).read_text(encoding='utf-8')))
            messages = write_output(report,args.output)
            print(f'Rendered {len(messages)} messages: {args.output}')
        elif args.command == 'schedule':
            settings.validate_live(send=True)
            print('Scheduler running: Sunday 19:00 Asia/Seoul; Ctrl+C to stop')
            def job(period):
                try:
                    execute_run(settings,period,'live',rules.CANDIDATE_LIMIT,
                        str(Path(args.output)/period.end.date().isoformat()))
                except DuplicateRunError:
                    logging.getLogger(__name__).info('Scheduled period already claimed; skipped')
            scheduler_loop(job)
        else:
            mode = 'dry-run' if args.dry_run else ('preview' if args.preview else 'live')
            if mode != 'dry-run' and (args.at or args.structure_only or args.resume_candidates):
                raise ValueError('--at, --structure-only and --resume-candidates require --dry-run')
            period = report_period(instant(args.at) if args.at else datetime.now(KST))
            result = execute_run(settings,period,mode,args.candidate_limit,args.output,
                args.structure_only,args.resume_candidates)
            print(dumps(result))
            return 0 if result.get('status','SUCCESS') == 'SUCCESS' else 2
        return 0
    except DuplicateRunError as error:
        print(str(error),file=sys.stderr)
        return 3
    except (ValueError,FileNotFoundError) as error:
        # Configuration exceptions contain setting names, never secret values.
        print(f'{type(error).__name__}: {error}',file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        logging.getLogger(__name__).error('CLI failed: %s',type(error).__name__)
        print(f'{type(error).__name__}: run failed; see logs/weekly_report.log and automation_runs',file=sys.stderr)
        return 1
    finally:
        if handler:
            logging.getLogger().removeHandler(handler)
            handler.close()
