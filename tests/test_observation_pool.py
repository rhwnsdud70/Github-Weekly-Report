from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from services.observation_pool import PoolRecord, PoolValidationError, build_pool, pool_manifest
from services.scheduler import report_period


NOW = datetime(2026,11,1,12,tzinfo=timezone.utc)


def record(index=1, **changes):
    if changes.get('critical_at') is not None:
        changes = {'independent_signals':('security',),
                   'event_refs':('https://github.com/advisories/GHSA-aaaa-bbbb-cccc',),**changes}
    return replace(PoolRecord(index,f'fixture/repo{index}',NOW-timedelta(days=1),NOW,
        report_period(NOW).end,verified=True,fixture=True),**changes)


def build(records, **changes):
    return build_pool(records,as_of=NOW,fixture=True,**changes)


def meaningful(index, **changes):
    return record(index,**{'meaningful_at':NOW-timedelta(days=1),'independent_signals':('activity',),**changes})


def test_order_independent_and_input_immutable():
    records = [record(3),meaningful(2),record(1,watchlisted_at=NOW)]
    original = deepcopy(records)
    results = [build(order) for order in permutations(records)]
    assert all(result == results[0] for result in results)
    assert records == original
    assert [entry.record.github_id for entry in results[0].entries] == [1,2,3]


def test_limit_and_exploration_do_not_displace_higher_priorities():
    result = build([meaningful(i) for i in range(1,43)]+[record(i) for i in range(50,70)])
    assert len(result.entries) == 40
    assert all(entry.priority != 5 for entry in result.entries)
    assert len(build([record(i) for i in range(1,21)]).entries) == 8


def test_merge_by_id_rename_and_signal_references_are_not_current_evidence():
    old = meaningful(1,full_name='old/name',last_seen_at=NOW-timedelta(hours=1))
    new = record(1,full_name='new/name',source_kind='pool')
    result = build([old,new])
    assert len(result.entries) == 1
    assert result.entries[0].record.full_name == 'new/name'
    assert result.entries[0].source_kinds == ('discovery','pool')
    assert 'current_evidence' not in pool_manifest(result,as_of=NOW,fixture=True)


@pytest.mark.parametrize('days,expected',[(7,1),(7.01,0)])
def test_discovery_ttl_endpoint(days,expected):
    assert len(build([record(first_seen_at=NOW-timedelta(days=days))]).entries) == expected


@pytest.mark.parametrize('days,expected',[(28,1),(28.01,0)])
def test_meaningful_ttl_not_renewed_by_metadata(days,expected):
    result = build([meaningful(1,first_seen_at=NOW-timedelta(days=40),
                              meaningful_at=NOW-timedelta(days=days))])
    assert len(result.entries) == expected


def test_four_completed_weeks_and_fourteen_day_deprioritization():
    boundary = report_period(NOW).end
    assert len(build([meaningful(1,source_period_end=boundary-timedelta(weeks=3))]).entries) == 1
    assert not build([meaningful(1,source_period_end=boundary-timedelta(weeks=4))]).entries
    entry = build([meaningful(1,first_seen_at=NOW-timedelta(days=20),
                              meaningful_at=NOW-timedelta(days=14))]).entries[0]
    assert entry.deprioritized and entry.priority == 4


def test_all_priority_tiers_and_critical_overflow():
    result = build([record(6),record(5,reported_at=NOW-timedelta(days=20)),meaningful(4),
                    record(3,reported_at=NOW),record(2,watchlisted_at=NOW),record(1,critical_at=NOW)])
    assert [entry.priority for entry in result.entries] == list(range(6))
    overflow = build([record(i,critical_at=NOW) for i in range(1,42)])
    assert overflow.status == 'CRITICAL_POOL_OVERFLOW' and overflow.entries == ()


def test_verified_failed_run_survives_unverified_and_unresolved_do_not():
    result = build([record(1,source_run_status='FAILED'),record(2,verified=False),record(None)])
    assert [entry.record.github_id for entry in result.entries] == [1]
    assert dict(result.diagnostics)['unverified'] == 1


@pytest.mark.parametrize('change',[
    {'last_seen_at':NOW+timedelta(seconds=1)}, {'source_period_end':NOW+timedelta(days=7)},
    {'fixture':False}, {'github_id':True}, {'first_seen_at':NOW+timedelta(seconds=1)},
])
def test_invalid_future_or_fixture_mixture_rejected_safely(change):
    with pytest.raises(PoolValidationError):
        build([record(**change)])


def test_tied_versions_are_deterministic_and_missing_requires_confirmed_count():
    versions = [record(1,source_run_status='FAILED'),record(1,source_run_status='SUCCESS')]
    assert build(versions) == build(reversed(versions))
    assert not build([record(confirmed_missing_count=3)]).entries
    assert build([record(confirmed_missing_count=1)]).entries


def test_production_blocks_fixture_and_manifest_is_detached():
    with pytest.raises(PoolValidationError,match='FIXTURE_MODE_MISMATCH'):
        build_pool([record()],as_of=NOW)
    production = build_pool([replace(record(),fixture=False)],as_of=NOW)
    assert production.entries
    result = build([record()])
    manifest = pool_manifest(result,as_of=NOW,fixture=True)
    manifest['entries'].clear()
    assert len(result.entries) == 1


def test_previous_pool_entries_are_accepted_without_renewing_ttl():
    prior = build([meaningful(1,first_seen_at=NOW-timedelta(days=40),meaningful_at=NOW-timedelta(days=28))])
    result = build(prior.entries)
    assert result.entries[0].expires_at == NOW
    assert result.entries[0].source_kinds == ('discovery','pool')
    later = build_pool(prior.entries,as_of=NOW+timedelta(seconds=1),fixture=True)
    assert not later.entries


def test_stale_critical_cannot_bypass_exclusion_and_unsupported_flag_is_rejected():
    assert not build([record(critical_at=NOW-timedelta(days=8),excluded=True)]).entries
    bad = replace(record(critical_at=NOW),independent_signals=(),event_refs=())
    with pytest.raises(PoolValidationError,match='UNSUPPORTED_CRITICAL_REFERENCE'):
        build([bad])


def test_empty_fixture_manifest_cannot_be_relabeled_production():
    result = build([])
    with pytest.raises(PoolValidationError,match='FIXTURE_MODE_MISMATCH'):
        pool_manifest(result,as_of=NOW)
    with pytest.raises(PoolValidationError,match='MANIFEST_AS_OF_MISMATCH'):
        pool_manifest(result,as_of=NOW+timedelta(seconds=1),fixture=True)


def test_waiting_observation_order_and_invalid_input_types():
    result = build([meaningful(2,last_observed_at=NOW-timedelta(hours=1)),
                    meaningful(1,last_observed_at=NOW-timedelta(days=1))])
    assert [entry.record.github_id for entry in result.entries] == [1,2]
    with pytest.raises(PoolValidationError):
        build([replace(record(),source_kind=['sensitive'])])


def test_latest_relevance_rejection_cannot_be_overridden_by_old_pool_input():
    old = meaningful(1,last_seen_at=NOW-timedelta(hours=1))
    latest = record(1,relevant=False)
    assert build([old,latest]).entries == build([latest,old]).entries == ()
