from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from config import rules
from database.transmission_log_store import transmission_logger, transmission_status
from models.domain import WeeklyReport
from models.serialization import dumps, candidate_from_dict
from services.candidate_service import CandidateCollector
from services.deep_analysis_service import DeepAnalysisCollector
from services.duplicate_service import merge_candidates, allow_repeat
from services.evidence_service import structure_evidence
from services.history_service import attach_history
from services.metric_service import collect_metrics
from services.narrative_service import NarrativeService
from services.persistence_service import persist_week, persist_measurements
from services.ranking_service import select_top
from services.score_service import score_candidate
from services.validation_service import validate_candidate
from telegram.formatter import format_report, weekly_trends
from telegram.splitter import split_messages

logger = logging.getLogger(__name__)


def write_output(report, output):
    directory = Path(output)
    directory.mkdir(parents=True,exist_ok=True)
    messages = split_messages(format_report(report))
    (directory/'report.json').write_text(dumps(report),encoding='utf-8')
    (directory/'messages.txt').write_text('\n\n===== MESSAGE =====\n\n'.join(m.text for m in messages),encoding='utf-8')
    return messages


class ReportPipeline:
    def __init__(self, database, github, web, llm=None, sender=None, clock=None):
        self.database, self.github, self.web = database, github, web
        self.llm, self.sender = llm, sender
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, period, mode='dry-run', limit=rules.CANDIDATE_LIMIT, output='artifacts/report',
            structure_only=False, candidate_cache=None):
        if mode not in {'live','preview','dry-run'}:
            raise ValueError('Unknown execution mode')
        if mode == 'live' and (self.llm is None or self.sender is None or structure_only):
            raise ValueError('Live mode requires LLM, sender and a complete report')
        if mode == 'preview' and self.sender is not None:
            raise ValueError('Preview must not have a sender')
        if not rules.TOP_COUNT <= limit <= 50:
            raise ValueError('Candidate limit must be between 5 and 50')
        if candidate_cache and mode != 'dry-run':
            raise ValueError('Candidate cache is only supported for offline dry-run validation')
        db = self.database
        start,end = period.start.isoformat(),period.end.isoformat()
        run_id = db.claim_live_run(start,end) if mode == 'live' else db.start_run(start,end,mode)
        candidates = []
        collector = CandidateCollector(self.github,self.web)
        measured_at = self.clock().isoformat()
        try:
            # Trending is a live rolling page, not a historical API. Do not apply today's rank to an old week.
            current_age = (self.clock()-period.end).total_seconds()
            include_trending = mode == 'dry-run' or 0 <= current_age <= rules.SNAPSHOT_BOUNDARY_TOLERANCE_SECONDS
            if not include_trending:
                collector.errors.append('Historical Trending unavailable; live page excluded from past report')
            if candidate_cache:
                cached = json.loads(Path(candidate_cache).read_text(encoding='utf-8'))
                if cached.get('period_end') != end or cached.get('mode') != 'dry-run':
                    raise ValueError('Candidate cache period or mode mismatch')
                candidates = [candidate_from_dict(c) for c in cached['candidates']]
            else:
                watched = db.rows('SELECT data FROM watchlist WHERE period_end=?',(start,))
                names = [json.loads(row['data'])['name'] for row in watched]
                candidates = merge_candidates(collector.collect(period,limit,include_trending,names))
            for candidate in candidates:
                if not candidate_cache:
                    attach_history(candidate,db,period,measured_at)
                    collect_metrics(candidate,self.github,period)
                validate_candidate(candidate,period)
                score_candidate(candidate)
                if not candidate.validation_reasons:
                    allow_repeat(candidate,period)
            Path(output).mkdir(parents=True,exist_ok=True)
            (Path(output)/'candidates.json').write_text(dumps({'mode':mode,'period_start':start,
                'period_end':end,'candidates':candidates}),encoding='utf-8')
            top,watchlist = select_top(candidates,period)
            if structure_only:
                result = {'run_id':run_id,'top':[c.repository.full_name for c in top],
                          'scores':[c.score.to_dict() for c in top],'candidate_count':len(candidates)}
                (Path(output)/'selection.json').write_text(dumps(result),encoding='utf-8')
                db.finish_run(run_id,'SUCCESS',result)
                return result
            documents = DeepAnalysisCollector(self.github).collect_top(top,period)
            narrator = NarrativeService(self.llm)
            for candidate in top:
                structure_evidence(candidate,documents[candidate.repository.identity])
                narrator.generate(candidate)
            reliability = list(collector.errors)
            reliability += [f'{candidate.repository.full_name}: {error}' for candidate in top for error in candidate.errors]
            if mode == 'dry-run':
                reliability.insert(0,'DRY RUN: 모든 프로젝트·수치·사건·LLM 응답은 합성 테스트 Fixture이며 실제 GitHub 뉴스가 아닙니다.')
            report = WeeklyReport(period,top,watchlist,reliability=reliability)
            report.trends = weekly_trends(report)
            messages = write_output(report,output)
            results = []
            if self.sender:
                self.sender.log = transmission_logger(db,run_id)
                results = self.sender.send(messages)
            status = transmission_status(results)
            if status == 'SUCCESS' and any('narrative degraded' in e for c in top for e in c.errors):
                status = 'PARTIAL_SUCCESS'
            persist_week(db,candidates,report,measured_at,status)
            result = {'run_id':run_id,'mode':mode,'status':status,'candidate_count':len(candidates),
                      'top':[c.repository.full_name for c in top],'message_count':len(messages),
                      'transmissions':json.loads(dumps(results)),'output':str(output)}
            db.finish_run(run_id,status,result)
            logger.info('Run %s: %s, candidates=%s, messages=%s',run_id,status,len(candidates),len(messages))
            return result
        except Exception as error:
            # A first run may lack sufficient history; retain actual observations for next week.
            try:
                if candidates:
                    persist_measurements(db,candidates,period,measured_at)
            except Exception as storage_error:
                logger.error('Observation recovery failed: %s',type(storage_error).__name__)
            try:
                db.finish_run(run_id,'FAILED',error=f'{type(error).__name__}: report not completed; see diagnostics')
            except Exception as storage_error:
                logger.error('Failed status could not be stored: %s',type(storage_error).__name__)
            logger.error('Run %s failed: %s',run_id,type(error).__name__)
            raise
