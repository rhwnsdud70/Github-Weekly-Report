"""Immutable observation contracts. No clients, database access, or implicit clock."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Producer:
    repository: str
    run_id: str
    attempt: int
    code_version: str


@dataclass(frozen=True)
class PoolReference:
    key: str
    sha256: str


@dataclass(frozen=True)
class SafeDiagnostics:
    category: str | None = None
    http_status: int | None = None
    rate_limit_resource: str | None = None
    rate_limit_limit: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    retry_after: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class RepositoryMetadata:
    github_id: int
    full_name: str
    html_url: str
    created_at: datetime
    description: str = ''
    topics: tuple[str, ...] = ()
    language: str | None = None
    pushed_at: datetime | None = None
    archived: bool = False
    disabled: bool = False


@dataclass(frozen=True)
class RepositoryObservation:
    github_id: int
    source_url: str
    status: str
    request_started_at: datetime | None
    response_received_at: datetime | None
    observed_at: datetime | None
    metadata: RepositoryMetadata | None = None
    stars: int | None = None
    forks: int | None = None
    source_type: str = 'github_repository_metadata'
    diagnostics: SafeDiagnostics = field(default_factory=SafeDiagnostics)


@dataclass(frozen=True)
class TrendingEntry:
    full_name: str
    rank: int
    github_id: int | None = None
    status: str = 'unresolved_identity'


@dataclass(frozen=True)
class TrendingObservation:
    status: str
    request_started_at: datetime | None
    response_received_at: datetime | None
    observed_at: datetime | None
    entries: tuple[TrendingEntry, ...] = ()
    page_sha256: str | None = None
    source_type: str = 'github_trending_weekly'
    source_url: str = 'https://github.com/trending?since=weekly'
    diagnostics: SafeDiagnostics = field(default_factory=SafeDiagnostics)


@dataclass(frozen=True)
class ObservationPayload:
    observation_id: str
    mode: str
    fixture: bool
    period_start: datetime
    boundary_end: datetime
    started_at: datetime
    completed_at: datetime
    producer: Producer
    pool_ref: PoolReference
    repositories: tuple[RepositoryObservation, ...]
    trending: TrendingObservation
    collection_status: str
    boundary_coverage: str
    publication_status: str = 'UNCONFIRMED'


@dataclass(frozen=True)
class Integrity:
    payload_sha256: str
    algorithm: str = 'sha256'


@dataclass(frozen=True)
class ObservationEnvelope:
    payload: ObservationPayload
    integrity: Integrity
    schema_version: int = 1


@dataclass(frozen=True)
class ObservationValidation:
    envelope: ObservationEnvelope
    eligible_repository_ids: tuple[int, ...]
    trending_eligible: bool
    ineligible_reasons: tuple[tuple[str, str], ...]
