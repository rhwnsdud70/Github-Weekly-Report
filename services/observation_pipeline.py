"""Small observation/report adapters. No clients, SQL or claims on import."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

from models.domain import Metric, WeeklySnapshot
from models.observation_context import ObservationContext
from models.serialization import dumps
from services.observation_consumer import consume_observations, legacy_from_weekly_snapshot, ObservationConsumerError
from services.observation_validation import ObservationValidationError
from services.r2_observation_store import R2ObservationStore, R2ObservationError


def production_observation_loader(settings):
    """Create a lazy loader; no R2 work before the claimed pipeline calls it."""
    def load(period, cutoff, legacy_snapshots):
        with R2ObservationStore(settings,as_of=cutoff,profile='report_read') as store:
            return consume_observations(store=store,period=period,cutoff=cutoff,
                                        legacy_snapshots=legacy_snapshots,prefer_object_snapshots=True)
    return load


def legacy_inputs(candidates, period):
    rows = []
    for candidate in candidates:
        repo = candidate.repository
        snapshot = candidate.previous.get('snapshot')
        if type(repo.github_id) is not int or repo.github_id <= 0 or not snapshot:
            continue
        try:
            rows.append(legacy_from_weekly_snapshot(WeeklySnapshot(repo.identity,period.start.isoformat(),
                snapshot['measured_at'],snapshot.get('stars'),snapshot.get('forks')),
                request_started_at=snapshot.get('request_started_at')))
        except (ValueError,KeyError,TypeError):
            candidate.previous.setdefault('snapshot_unusable_reasons',[]).append('invalid_legacy_snapshot')
    return tuple(rows)


def validate_pipeline_context(context, period, cutoff, *, namespace='production'):
    if not isinstance(context,ObservationContext):
        raise ObservationConsumerError('INVALID_OBSERVATION_CONTEXT')
    if namespace not in {'production','fixture'} or context.namespace != namespace:
        raise ObservationConsumerError('FIXTURE_NAMESPACE_MISMATCH')
    if (context.period_start != period.start or context.period_end != period.end
            or context.cutoff != cutoff):
        raise ObservationConsumerError('INVALID_OBSERVATION_CONTEXT')
    seen = set()
    for row in context.repositories:
        if type(row.github_id) is not int or row.github_id <= 0 or row.github_id in seen:
            raise ObservationConsumerError('REPOSITORY_ID_MISMATCH')
        seen.add(row.github_id)
        for selection,boundary in ((row.current,period.end),(row.previous,period.start)):
            sample = selection.snapshot
            if sample and (sample.github_id != row.github_id or sample.boundary != boundary):
                raise ObservationConsumerError('REPOSITORY_ID_MISMATCH')
    return context


def safe_observation_failure(error):
    if isinstance(error,(R2ObservationError,ObservationConsumerError,ObservationValidationError)):
        return {'category':error.category,'http_status':getattr(error,'http_status',None)}
    return {'category':'OBSERVATION_LOAD_FAILED','http_status':None}


def apply_observation(candidate, context, measured_at, *, unavailable=False, load_error=None):
    """Preserve latest sample; evaluate boundary samples without mixing sources."""
    latest = deepcopy(candidate.repository)
    candidate.latest_repository = latest
    candidate.latest_observed_at = measured_at
    identity = latest.github_id
    row = (context.for_repository(identity) if context is not None
           and type(identity) is int and identity > 0 else None)
    current = row.current.snapshot if row and row.current.status == 'CONFIRMED' else None
    # Keep discovery relevance/category/current canonical name for API routing.
    # T and age used for evaluation use the boundary sample where available.
    candidate.repository = replace(latest,stars=current.stars if current else None,
        forks=current.forks if current else None,
        created_at=(current.metadata.created_at.isoformat() if current and current.metadata else latest.created_at))
    for name in ('star_delta','star_growth','fork_delta','fork_growth'):
        candidate.metrics[name] = getattr(row.comparison,name) if row else Metric(source='boundary observation missing/unavailable')
    candidate.metrics['trending'] = (Metric.known(True,'stored weekly Trending observation')
        if row and row.trending_rank.status == 'confirmed' else Metric(source='stored Trending missing/unavailable'))
    candidate.metrics['trending_rank'] = row.trending_rank if row else Metric()
    candidate.metrics['trending_clearly_top'] = Metric()
    candidate.observation = json.loads(dumps({
        'enabled':True,'context_loaded':row is not None,
        'namespace':context.namespace if context else None,
        'status':'UNAVAILABLE' if unavailable else 'LOADED' if context is not None else 'MISSING',
        'repository':row,'trending':context.trending if context else None,
        'cutoff':context.cutoff if context else None,
        'diagnostics':([asdict(d) for d in context.diagnostics if d.github_id in {None,identity}] if context else
                       [load_error] if load_error else []),
    }))


def observation_counts(candidates):
    def selected(c, key):
        return (c.observation.get('repository') or {}).get(key) or {}
    return {
        'observation_context_loaded':sum(c.observation.get('context_loaded') is True for c in candidates),
        'observation_context_missing':sum(c.observation.get('context_loaded') is not True for c in candidates),
        'current_boundary_confirmed':sum(selected(c,'current').get('status') == 'CONFIRMED' for c in candidates),
        'previous_boundary_confirmed':sum(selected(c,'previous').get('status') == 'CONFIRMED' for c in candidates),
        'star_comparison_confirmed':sum(c.metric('star_delta').status == 'confirmed' for c in candidates),
        'trending_confirmed':sum(c.metric('trending').value is True for c in candidates),
        'observation_conflicted':sum(any(selected(c,k).get('status') == 'CONFLICT' for k in ('current','previous'))
                                   or (c.observation.get('trending') or {}).get('status') == 'CONFLICT' for c in candidates),
        'observation_unavailable':sum(c.observation.get('status') == 'UNAVAILABLE' or
            any(selected(c,k).get('status') == 'UNAVAILABLE' for k in ('current','previous')) for c in candidates),
        'legacy_snapshot_used':sum(any((selected(c,k).get('snapshot') or {}).get('provenance',{}).get('source_type')
            == 'legacy_sqlite_snapshot' for k in ('current','previous')) for c in candidates),
    }
