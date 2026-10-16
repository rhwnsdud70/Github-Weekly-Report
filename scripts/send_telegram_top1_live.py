"""Send one saved TOP 1 artifact to Telegram without GitHub/OpenAI/weekly persistence."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import Settings
from database.db import Database
from database.transmission_log_store import transmission_logger, transmission_status
from models.domain import Candidate, Event, Repository
from telegram.formatter import top1_test_message
from telegram.sender import TelegramSender
from telegram.splitter import split_message, units


DEFAULT_ARTIFACT = Path('artifacts/openai-top1-test')
DEFAULT_TEST_DB = Path('data/telegram_top1_test.db')
EXPECTED_REPOSITORY = 'damianvtran/local-operator'


def load_candidate(artifact_dir=DEFAULT_ARTIFACT):
    artifact_dir = Path(artifact_dir)
    metadata = json.loads((artifact_dir/'run_metadata.json').read_text(encoding='utf-8'))
    if metadata.get('validation_status') != 'SUCCESS' or metadata.get('repository') != EXPECTED_REPOSITORY:
        raise ValueError('No matching successful OpenAI TOP 1 artifact')
    source = json.loads((artifact_dir/'source_evidence.json').read_text(encoding='utf-8'))
    narrative = json.loads((artifact_dir/'final_narrative.json').read_text(encoding='utf-8'))
    candidate = Candidate(Repository(**source['repository']), {'saved_openai_top1_artifact'})
    candidate.narrative = narrative
    candidate.evidence = {'important_links':source.get('important_links', [])}
    notable = next((item for item in source.get('notable_events', [])
                    if item.get('source_type') == 'Release'), None)
    if notable:
        candidate.events.append(Event('release', notable['text'], notable['url'],
                                      notable['occurred_at'], 'release'))
    return candidate, metadata


def build_messages(artifact_dir=DEFAULT_ARTIFACT):
    candidate, metadata = load_candidate(artifact_dir)
    return split_message(top1_test_message(candidate)), metadata


def run(settings, artifact_dir=DEFAULT_ARTIFACT, db_path=DEFAULT_TEST_DB, client=None, sleep=None):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise ValueError('Missing Telegram settings')
    messages, metadata = build_messages(artifact_dir)
    db = Database(db_path)
    run_id = db.start_run(metadata['period_start'], metadata['period_end'], 'telegram-test')
    sender = TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id,
                            client=client, sleep=sleep or __import__('time').sleep,
                            log=transmission_logger(db,run_id))
    try:
        results = sender.send(messages)
        status = transmission_status(results)
        report = {'test_run':True, 'telegram_test':True,
                  'artifact':str(Path(artifact_dir)/'final_narrative.json'),
                  'repository':EXPECTED_REPOSITORY, 'message_count':len(messages),
                  'transmissions':[asdict(result) for result in results]}
        db.finish_run(run_id,status,report)
        return messages, results, status, run_id
    finally:
        db.close()
        if client is None:
            sender.client.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-dir',type=Path,default=DEFAULT_ARTIFACT)
    parser.add_argument('--test-db',type=Path,default=DEFAULT_TEST_DB)
    args = parser.parse_args(argv)
    settings = Settings.load('.env')
    messages, results, status, run_id = run(settings,args.artifact_dir,args.test_db)
    print('Artifact source: ' + str(args.artifact_dir/'final_narrative.json'))
    print(f'Message count: {len(messages)}')
    for index,(message,result) in enumerate(zip(messages,results),1):
        print(f'Message {index}: characters={len(message.text)}, utf16_units={units(message.text)}, '
              f'attempts={result.attempts}, HTTP={result.http_status or "N/A"}, status={result.status}')
        if result.status == 'FAILED':
            print('Telegram error: ' + (result.description or result.error or 'unknown'))
    print(f'Test run ID: {run_id}')
    print(f'Final status: {status}')
    print('Telegram TOP1 test: SENT' if status == 'SUCCESS' else 'Telegram TOP1 test: FAILED')
    # A final message failure is a recorded test result, not a process crash.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
