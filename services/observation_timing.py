"""Existing observation-window policy and safe diagnostics; never moves a boundary."""
from config import rules
from models.domain import instant
from services.scheduler import KST


def observation_allowed(observed_at, boundary):
    offset = (instant(observed_at)-instant(boundary)).total_seconds()
    return 0 <= offset <= rules.SNAPSHOT_BOUNDARY_TOLERANCE_SECONDS


def format_observation_timing(period, started_at, checked_at):
    """Preflight temporal eligibility only, not a claim that source data exists."""
    seconds = (instant(checked_at)-period.end).total_seconds()
    allowed = observation_allowed(checked_at,period.end)
    total = int(abs(seconds))
    hours, remainder = divmod(total,3600)
    minutes, seconds_part = divmod(remainder,60)
    elapsed = f'{hours}h {minutes}m {seconds_part}s'
    reason = ('eligible' if allowed else 'ineligible (before report boundary)' if seconds < 0 else
              f'ineligible (outside {rules.SNAPSHOT_BOUNDARY_TOLERANCE_SECONDS}s boundary tolerance)')
    lines = [
        f'Scheduled report boundary: {period.end.astimezone(KST).isoformat()} (Sunday 19:00 KST)',
        f'Actual runner start: {instant(started_at).astimezone(KST).isoformat()}',
        f'Observation preflight time: {instant(checked_at).astimezone(KST).isoformat()}',
        f'Collection delay: {elapsed} ({seconds:g}s after report end; includes restore time)',
        f'Trending observation: {reason}',
        f'Snapshot observation: {reason}',
        'Snapshot comparison: also requires a previous snapshot within its boundary tolerance',
        'Observation eligibility: preflight only; Pipeline rechecks actual observation time',
    ]
    lines.append('Unavailable due to timing: '+('none' if allowed else
        'live Trending; current Star/Fork snapshot comparisons (values remain unknown)'))
    return '\n'.join(lines)
