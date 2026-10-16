from scripts.run_openai_top1_live import PERIOD, REPOSITORY, source_manifest_matches


def test_saved_source_requires_current_schema_repository_and_period():
    valid = {'source_manifest': {'schema_version':2, 'repository':REPOSITORY,
        'period_start':PERIOD.start.isoformat(), 'period_end':PERIOD.end.isoformat()}}
    assert source_manifest_matches(valid)
    assert not source_manifest_matches({})
    stale = {'source_manifest': dict(valid['source_manifest'], period_end='2026-01-01T00:00:00+09:00')}
    assert not source_manifest_matches(stale)
