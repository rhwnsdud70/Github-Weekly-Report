"""One production run with R2 restore/finalize/upload; never a scheduler loop.

The caller must serialize the ENTIRE invocation across machines. The local SQLite
claim is not a distributed R2 lock. No upload runs in a finally/atexit handler.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from collectors.github_client import GitHubClient
from config import rules
from database.db import Database, DuplicateRunError
from database.sqlite_backup import SQLiteBackupError, checkpoint_and_close, validate_database
from services.narrative_service import OpenAIJSONClient
from services.pipeline import ReportPipeline
from services.r2_persistence import R2DatabaseStore, R2PersistenceError
from services.ranking_service import InsufficientCandidates
from services.candidate_diagnostics import candidate_diagnostic_summary, format_candidate_diagnostics
from services.scheduler import KST, report_period
from services.observation_timing import format_observation_timing
from services.observation_pipeline import production_observation_loader
from services.candidate_diagnostics import observation_diagnostic_summary, format_observation_diagnostics
from telegram.sender import TelegramSender


@dataclass(frozen=True)
class OneShotResult:
    status: str
    exit_code: int
    pipeline_status: str | None = None
    remote_updated: bool = False
    duplicate_delivery_risk: bool = False


@contextmanager
def production_pipeline(settings, database, clock):
    """Create production clients only after restore and duplicate-claim preflight."""
    with httpx.Client(timeout=30) as client, httpx.Client(timeout=90) as llm_client:
        github = GitHubClient(settings.github_token,client)
        llm = OpenAIJSONClient(settings.openai_api_key,settings.openai_model,llm_client)
        sender = TelegramSender(settings.telegram_bot_token,settings.telegram_chat_id,client)
        yield ReportPipeline(database,github,client,llm,sender,clock=clock,
                             observation_loader=production_observation_loader(settings))


def period_claims(database, period):
    return database.rows(
        'SELECT run_id,status,finished_at FROM automation_runs '
        'WHERE mode=? AND julianday(period_end)=julianday(?)',
        ('live',period.end.isoformat()))


def terminal_claim_confirmed(database, period, status, run_id=None):
    claims = period_claims(database,period)
    return (len(claims) == 1 and claims[0]['status'] == status and
            bool(claims[0]['finished_at']) and
            (run_id is None or claims[0]['run_id'] == run_id))


def safe_failure(error):
    """Only trusted categorical diagnostics, never arbitrary exception text/traceback."""
    if isinstance(error,R2PersistenceError):
        return f'{error.category}; HTTP={error.http_status or "N/A"}'
    if isinstance(error,SQLiteBackupError):
        return 'SQLITE_VALIDATION_OR_CHECKPOINT_ERROR'
    if isinstance(error,ValueError):
        return 'INVALID_OR_MISSING_CONFIGURATION'
    return 'UNEXPECTED_ERROR'


def run_weekly_live_once(settings, *, allow_initial_bootstrap=False, output='artifacts/weekly-live-once',
                         clock=None, store=None, pipeline_factory=None, emit=print):
    """Production-only orchestration. Factories/clock are injectable for offline tests.

    SUCCESS=0, duplicate skip=0, application FAILED=1, PARTIAL_SUCCESS=2,
    configuration/restore/bootstrap blocked=3, finalize/state invalid=4,
    upload failure=5, unexpected failure=6, interrupted=130.
    """
    clock = clock or (lambda:datetime.now(KST))
    pipeline_status = None
    pipeline_started = False
    emit('Mode: production-live-once')
    emit('Concurrency: serialize restore/run/upload externally; no distributed R2 lock')

    def finish(status, code, remote_updated=False, risk=False):
        if risk:
            emit('Recovery: remote claim not confirmed; duplicate delivery possible. '
                 'Retain local SQLite, WAL and pipeline artifacts; inspect before retry. Do not auto-resend.')
        emit(f'Final status: {status}; exit_code={code}')
        return OneShotResult(status,code,pipeline_status,remote_updated,risk)

    try:
        settings.validate_r2()
        emit('R2 configuration: SUCCESS')
        settings.validate_live(send=True)
        emit('Production configuration: SUCCESS')
        # Pin the scheduler boundary once, even if restoring takes a long time.
        started_at = clock()
    except Exception as error:
        emit('Configuration: FAILED; '+safe_failure(error))
        return finish('CONFIGURATION_FAILED',3)

    try:
        with (store if store is not None else R2DatabaseStore(settings)) as persistence:
            try:
                restored = persistence.restore()
            except Exception as error:
                emit('R2 restore: FAILED; '+safe_failure(error))
                return finish('RESTORE_FAILED',3)
            if not restored:
                emit('R2 restore: OBJECT_MISSING')
                if not allow_initial_bootstrap:
                    emit('Bootstrap: BLOCKED; explicit --allow-initial-bootstrap required')
                    return finish('BOOTSTRAP_REQUIRED',3)
                emit('Bootstrap: explicitly allowed; initialize production schema')
            else:
                emit('R2 restore: SUCCESS')

            period = report_period(started_at)
            emit(f'Period start: {period.start.isoformat()}')
            emit(f'Period end: {period.end.isoformat()}')
            emit('Period rule: latest completed Sunday 19:00 KST; start <= event < end; 168 hours')
            emit(format_observation_timing(period,started_at,clock()))

            database = Database(settings.database_path)
            try:
                # Defense in depth for restored state before opening any API clients.
                validate_database(settings.database_path)
                if period_claims(database,period):
                    emit('Production Claim: ALREADY_CLAIMED; blocked before GitHub/OpenAI/Telegram')
                    emit('Pipeline: SKIPPED; R2 upload: SKIPPED (existing claim unchanged)')
                    return finish('SKIPPED_ALREADY_CLAIMED',0)
                emit('Production Claim: AVAILABLE; atomic claim belongs to existing Pipeline')
                factory = pipeline_factory or (lambda db:production_pipeline(settings,db,clock))
                try:
                    with factory(database) as pipeline:
                        pipeline_started = True
                        result = pipeline.run(period,mode='live',limit=rules.CANDIDATE_LIMIT,
                                              output=Path(output)/period.end.date().isoformat())
                except DuplicateRunError:
                    emit('Production Claim: ALREADY_CLAIMED (atomic guard); Pipeline/send blocked')
                    emit('R2 upload: SKIPPED (existing claim unchanged)')
                    return finish('SKIPPED_ALREADY_CLAIMED',0)
                except InsufficientCandidates as error:
                    pipeline_status = 'FAILED'
                    emit('Pipeline: FAILED; InsufficientCandidates (handled application failure)')
                    diagnostic = candidate_diagnostic_summary(getattr(error,'diagnostics',None))
                    observation = observation_diagnostic_summary(getattr(error,'observation_diagnostics',None))
                    if observation is not None:
                        emit(format_observation_diagnostics(observation))
                    if diagnostic is not None:
                        emit(format_candidate_diagnostics(diagnostic))
                    else:
                        emit('Candidate diagnostics: UNAVAILABLE; no aggregate payload supplied; counts not inferred')
                    terminal = terminal_claim_confirmed(database,period,'FAILED')
                else:
                    pipeline_status = result.get('status')
                    terminal = (pipeline_status in {'SUCCESS','PARTIAL_SUCCESS','FAILED'} and
                                result.get('mode') == 'live' and bool(result.get('run_id')) and
                                terminal_claim_confirmed(database,period,pipeline_status,result['run_id']))
                    # Never echo an unvalidated result payload, including arbitrary strings.
                    emit('Pipeline: '+(pipeline_status if terminal else 'INVALID_TERMINAL_STATE'))
                    observation = observation_diagnostic_summary(result.get('observation_diagnostics'))
                    if observation is not None:
                        emit(format_observation_diagnostics(observation))
                if not terminal:
                    emit('SQLite finalize/integrity: BLOCKED; terminal claim not confirmed')
                    emit('R2 upload: BLOCKED')
                    return finish('RUN_STATE_INVALID',4,risk=True)
                emit('Production Claim: RECORDED; terminal state confirmed')
                try:
                    # The pipeline must finish its own transactions. Never silently commit
                    # unknown partial work, including a swallowed storage failure.
                    checkpoint_and_close(database.connection)
                    validate_database(settings.database_path)
                except Exception as error:
                    emit('SQLite finalize/integrity: FAILED; '+safe_failure(error))
                    emit('R2 upload: BLOCKED')
                    return finish('SQLITE_FINALIZE_FAILED',4,risk=True)
                emit('SQLite finalize/integrity: SUCCESS; connection closed')
            finally:
                database.close()

            try:
                persistence.upload()  # Existing module exports standalone DB and backs up remote first.
            except Exception as error:
                emit('R2 upload: FAILED; '+safe_failure(error))
                return finish('R2_UPLOAD_FAILED',5,risk=True)
            emit('R2 upload: SUCCESS; production claim preserved remotely')
            if pipeline_status == 'SUCCESS':
                return finish('SUCCESS',0,remote_updated=True)
            if pipeline_status == 'PARTIAL_SUCCESS':
                return finish('PARTIAL_SUCCESS',2,remote_updated=True)
            return finish('APPLICATION_FAILED',1,remote_updated=True)
    except KeyboardInterrupt:
        emit('Pipeline/runner: INTERRUPTED; remote completion unconfirmed; no recovery upload attempted')
        return finish('INTERRUPTED',130,risk=pipeline_started)
    except Exception as error:
        emit('Pipeline/runner: FAILED; '+safe_failure(error))
        emit('R2 upload: NOT_ATTEMPTED after unexpected failure; retain local recovery state')
        return finish('UNEXPECTED_FAILURE',6,risk=pipeline_started)
