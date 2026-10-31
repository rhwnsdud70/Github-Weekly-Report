from dataclasses import asdict, is_dataclass
from datetime import datetime
import json
from models.domain import Candidate, Repository, Event, Metric, ScoreResult, WeeklyReport, ReportPeriod, instant


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value,set):
        return sorted(value)
    if isinstance(value,datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def dumps(value):
    return json.dumps(value,ensure_ascii=False,indent=2,default=json_default)


def candidate_from_dict(raw):
    raw = dict(raw)
    raw['repository'] = Repository(**raw['repository'])
    if raw.get('latest_repository') is not None:
        raw['latest_repository'] = Repository(**raw['latest_repository'])
    raw['events'] = [Event(**event) for event in raw['events']]
    raw['metrics'] = {key:Metric(**metric) for key,metric in raw['metrics'].items()}
    raw['score'] = ScoreResult(**raw['score']) if raw['score'] else None
    for key in ('collection_sources','signals'):
        raw[key] = set(raw[key])
    return Candidate(**raw)


def report_from_dict(raw):
    period = ReportPeriod(instant(raw['period']['start']),instant(raw['period']['end']))
    return WeeklyReport(period,[candidate_from_dict(c) for c in raw['top']],
        [candidate_from_dict(c) for c in raw['watchlist']],raw['trends'],raw['reliability'])
