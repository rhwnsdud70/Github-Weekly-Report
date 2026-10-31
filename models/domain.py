from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError('Timezone-aware timestamp required')
    return result


@dataclass(frozen=True)
class Metric:
    value: Any = None
    status: str = 'unknown'
    source: str = ''

    def __post_init__(self):
        if self.status not in {'confirmed', 'unknown', 'unavailable', 'not_applicable'}:
            raise ValueError('Invalid metric status')
        if (self.value is not None) != (self.status == 'confirmed'):
            raise ValueError('Only confirmed metrics can have values')

    @classmethod
    def known(cls, value, source=''):
        return cls(value, 'confirmed', source)


@dataclass
class Repository:
    github_id: int | None
    full_name: str
    url: str
    description: str = ''
    category: str = 'Developer Tools'
    language: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    stars: int | None = None
    forks: int | None = None
    topics: list[str] = field(default_factory=list)
    relevant: bool = True
    excluded_reason: str = ''

    @property
    def identity(self):
        if self.github_id is not None:
            return f'id:{self.github_id}'
        if self.url:
            return 'url:' + self.url.rstrip('/').removesuffix('.git').lower()
        return 'name:' + self.full_name.lower()


@dataclass
class Event:
    kind: str
    title: str
    url: str
    occurred_at: str
    source: str
    details: dict = field(default_factory=dict)

    @property
    def identity(self):
        return (self.kind, self.url)


@dataclass
class ScoreResult:
    components: dict[str, int | None] = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    @property
    def total(self):
        return sum(value for value in self.components.values() if value is not None)

    @property
    def grade(self):
        from config.rules import GRADE_BUCKETS
        return next(label for lower, label in GRADE_BUCKETS if self.total >= lower)

    def to_dict(self):
        return {**asdict(self), 'total': self.total, 'grade': self.grade}


@dataclass
class Candidate:
    repository: Repository
    collection_sources: set[str] = field(default_factory=set)
    events: list[Event] = field(default_factory=list)
    metrics: dict[str, Metric] = field(default_factory=dict)
    score: ScoreResult | None = None
    previous: dict = field(default_factory=dict)
    signals: set[str] = field(default_factory=set)
    validation_reasons: list[str] = field(default_factory=list)
    changes_since_report: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    selection_reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    narrative: dict = field(default_factory=dict)
    # Evaluation boundary sample and the actual latest sample remain separate.
    latest_repository: Repository | None = None
    latest_observed_at: str | None = None
    observation: dict = field(default_factory=dict)

    def metric(self, name):
        return self.metrics.get(name, Metric())

    @property
    def critical(self):
        from config.rules import CRITICAL_EVENTS
        return any(event.kind in CRITICAL_EVENTS and event.source == 'security' for event in self.events)


@dataclass
class WeeklySnapshot:
    identity: str
    period_end: str
    measured_at: str
    stars: int | None
    forks: int | None
    issues: int | None = None
    pulls: int | None = None
    discussions: int | None = None


@dataclass(frozen=True)
class ReportPeriod:
    start: datetime
    end: datetime

    def contains(self, timestamp):
        return self.start <= instant(timestamp) < self.end


@dataclass
class WeeklyReport:
    period: ReportPeriod
    top: list[Candidate]
    watchlist: list[Candidate] = field(default_factory=list)
    trends: list[str] = field(default_factory=list)
    reliability: list[str] = field(default_factory=list)


@dataclass
class TelegramMessage:
    kind: str
    header: str
    sections: list[str]
    rank: int | None = None
    part: int = 1

    @property
    def text(self):
        return '\n\n'.join([self.header, *self.sections])
