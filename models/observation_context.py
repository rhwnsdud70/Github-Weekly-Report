"""Frozen read-only consumer results. No database, clients or scoring mutation."""
from dataclasses import dataclass
from datetime import datetime

from models.domain import Metric
from models.observation import RepositoryMetadata, TrendingEntry


@dataclass(frozen=True)
class ObservationProvenance:
    source_type: str
    source_url: str
    object_key: str | None
    object_id: str
    sha256: str | None
    request_started_at: datetime
    observed_at: datetime
    timestamp_precision: str = 'individual_response'


@dataclass(frozen=True)
class BoundarySnapshot:
    github_id: int
    boundary: datetime
    stars: int | None
    forks: int | None
    metadata: RepositoryMetadata | None
    provenance: ObservationProvenance


@dataclass(frozen=True)
class LegacySnapshot:
    """Detached row adapter input; absent request time must never be fabricated.

    A real Phase5B adapter must supply separately recorded request provenance to
    make a legacy row eligible. Existing WeeklySnapshot alone cannot prove it.
    """
    github_id: int
    boundary: datetime
    measured_at: datetime
    stars: int | None
    forks: int | None
    request_started_at: datetime | None = None
    fixture: bool = False


@dataclass(frozen=True)
class LatestMetadata:
    metadata: RepositoryMetadata
    observed_at: datetime
    stars: int | None
    forks: int | None
    fixture: bool = False


@dataclass(frozen=True)
class ConsumerDiagnostic:
    category: str
    boundary: datetime | None = None
    github_id: int | None = None
    object_key: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class SnapshotSelection:
    status: str  # CONFIRMED / MISSING / CONFLICT / UNAVAILABLE
    snapshot: BoundarySnapshot | None = None
    conflict_sources: tuple[ObservationProvenance, ...] = ()


@dataclass(frozen=True)
class SnapshotComparison:
    star_delta: Metric
    star_growth: Metric
    fork_delta: Metric
    fork_growth: Metric
    observed_interval_seconds: float | None


@dataclass(frozen=True)
class TrendingSelection:
    status: str
    boundary: datetime
    entries: tuple[TrendingEntry, ...] = ()
    page_sha256: str | None = None
    provenance: ObservationProvenance | None = None
    conflict_sources: tuple[ObservationProvenance, ...] = ()


@dataclass(frozen=True)
class RepositoryObservationContext:
    github_id: int
    current: SnapshotSelection
    previous: SnapshotSelection
    latest: LatestMetadata | None
    comparison: SnapshotComparison
    trending_rank: Metric


@dataclass(frozen=True)
class BoundaryCoverage:
    boundary: datetime
    status: str  # COMPLETE / EMPTY / UNAVAILABLE (read coverage, not collection status)
    listed_objects: int
    loaded_objects: int
    eligible_repositories: int


@dataclass(frozen=True)
class CoverageSummary:
    repository_count: int
    current_confirmed: int
    previous_confirmed: int
    star_comparisons_confirmed: int
    conflicted_repositories: int
    unavailable_repositories: int
    missing_repositories: int
    trending_status: str


@dataclass(frozen=True)
class ObservationContext:
    period_start: datetime
    period_end: datetime
    cutoff: datetime
    namespace: str
    repositories: tuple[RepositoryObservationContext, ...]
    trending: TrendingSelection
    coverage: tuple[BoundaryCoverage, ...]
    diagnostics: tuple[ConsumerDiagnostic, ...]
    summary: CoverageSummary

    def for_repository(self, github_id: int) -> RepositoryObservationContext | None:
        return next((r for r in self.repositories if r.github_id == github_id),None)
