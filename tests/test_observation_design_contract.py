"""Check the proposed time contract with existing policy, not a live consumer.

Pool/store/producer/consumer fault tests remain planned in OBSERVATION_ARCHITECTURE.
No new runtime service or SDK client is constructed by these fixtures.
"""
from datetime import timedelta
import json
from pathlib import Path

import pytest

from models.domain import instant
from services.observation_timing import observation_allowed
from services.scheduler import report_period


FIXTURE = json.loads((Path(__file__).parent/'fixtures'/'observation_design_time_cases.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('case',FIXTURE['cases'],ids=lambda case:case['name'])
def test_proposed_snapshot_time_contract_uses_actual_observations(case):
    assert FIXTURE['fixture'] is True
    boundary = instant(FIXTURE['boundary_end'])
    cutoff = boundary+timedelta(seconds=case['report_delay'])
    period = report_period(cutoff)
    assert period.end == boundary
    assert period.end-period.start == timedelta(hours=168)

    def eligible(interval, source_boundary):
        if interval is None:
            return False
        request = source_boundary+timedelta(seconds=interval[0])
        response = source_boundary+timedelta(seconds=interval[1])
        return (request <= response <= cutoff and
                observation_allowed(request,source_boundary) and
                observation_allowed(response,source_boundary))

    valid_pair = eligible(case['previous'],period.start) and eligible(case['current'],period.end)
    assert valid_pair is case['eligible']
