from datetime import timedelta
from config import rules
from collectors.github_client import GitHubError
from collectors.github_release import release_event
from models.domain import Metric, ReportPeriod, instant


def growth(current, previous, source):
    if current is None or previous is None:
        return Metric(source=source)
    if previous == 0:
        return Metric(None, 'not_applicable', source)
    return Metric.known((current - previous) / previous * 100, source)


def query_window(period):
    return f'created:{period.start.isoformat()}..{(period.end-timedelta(seconds=1)).isoformat()}'


def _previous_activity(candidate, counts=None, error=None):
    # Previous week is persisted with the snapshot. Never spend two Search API
    # requests per repository to rediscover data that this service already owns.
    snapshot = (candidate.previous.get('snapshot') or {}) if candidate.previous.get('snapshot_usable', True) else {}
    for kind in ('issues', 'pulls'):
        label = 'previous_' + kind
        value = counts.get(kind) if counts else None
        candidate.metrics[label] = (Metric.known(value, 'GitHub GraphQL exact previous-period count')
                                    if value is not None else Metric(None, 'unavailable',
                                        str(error or 'GraphQL previous-period result incomplete')))


def _finish_metrics(candidate, github, period, counts=None, previous_counts=None, error=None, previous_error=None):
    name = candidate.repository.full_name
    _previous_activity(candidate, previous_counts, previous_error)
    for kind in ('issues', 'pulls'):
        if counts is not None and counts.get(kind) is not None:
            candidate.metrics[kind] = Metric.known(counts[kind], 'GitHub GraphQL exact createdAt count')
        else:
            reason = error or 'GraphQL current-period result incomplete'
            candidate.metrics[kind] = Metric(None, 'unavailable', str(reason))
            candidate.errors.append(f'{kind}: {reason}')
    try:
        fetch = getattr(github, 'releases_for_period', None)
        releases = fetch(name, period.start, period.end) if fetch else github.releases(name)
        events = [release_event(item) for item in releases]
        for event in events:
            if event and period.contains(event.occurred_at) and event.identity not in {e.identity for e in candidate.events}:
                candidate.events.append(event)
                candidate.collection_sources.add('release')
        candidate.metrics['releases_checked'] = Metric.known(True, 'GitHub releases')
    except GitHubError as release_error:
        candidate.metrics['releases_checked'] = Metric(None, 'unavailable', str(release_error))
        candidate.errors.append(f'releases: {release_error}')
    # Discussion bodies/counts are deep-analysis data. Candidate ranking records
    # this component as unknown rather than consuming one GraphQL request each.
    candidate.metrics['discussion_total'] = Metric(source='deferred until final TOP 5')
    calculate_metrics(candidate, period)


def collect_metrics_batch(candidates, github, period):
    """Populate minimal candidate metrics with one GraphQL activity request."""
    names = [candidate.repository.full_name for candidate in candidates]
    activity_error = previous_error = None
    try:
        activity = github.activity_counts(names, period.start, period.end)
    except GitHubError as error:
        activity = {}
        activity_error = error
    previous_period = ReportPeriod(period.start-timedelta(days=7), period.start)
    try:
        previous_activity = github.activity_counts(names, previous_period.start, previous_period.end)
    except GitHubError as error:
        previous_activity = {}
        previous_error = error
    fallbacks = 0
    for candidate in candidates:
        current = activity.get(candidate.repository.full_name)
        if not current:
            continue
        for kind in ('issues', 'pulls'):
            if current.get(kind) is not None or fallbacks >= rules.ACTIVITY_FALLBACK_SEARCH_BUDGET:
                continue
            try:
                current[kind] = github.activity_count_fallback(candidate.repository.full_name, kind,
                                                               period.start, period.end)
                fallbacks += 1
            except GitHubError:
                fallbacks += 1
    for candidate in candidates:
        name = candidate.repository.full_name
        _finish_metrics(candidate, github, period, activity.get(name), previous_activity.get(name),
                        activity_error, previous_error)


def collect_metrics(candidate, github, period):
    """Compatibility entry point for one candidate; production uses the batch form."""
    collect_metrics_batch([candidate], github, period)


def calculate_metrics(candidate, period):
    metrics = candidate.metrics
    for kind in ('issues', 'pulls'):
        metrics[kind + '_growth'] = growth(candidate.metric(kind).value,
            candidate.metric('previous_' + kind).value, 'weekly activity')
    current = [candidate.metric(k).value for k in ('issues', 'pulls')]
    previous = [candidate.metric('previous_' + k).value for k in ('issues', 'pulls')]
    # Combined Issue + PR counts: distinct GitHub is:issue / is:pr searches.
    metrics['activity'] = Metric.known(sum(current), 'issues + PRs') if all(x is not None for x in current) else Metric()
    metrics['activity_growth'] = growth(metrics['activity'].value,
        sum(previous) if all(x is not None for x in previous) else None, 'combined weekly activity')
    snapshot = (candidate.previous.get('snapshot') or {}) if candidate.previous.get('snapshot_usable',True) else {}
    forks = candidate.repository.forks
    if not candidate.observation.get('enabled'):
        metrics['fork_delta'] = (Metric.known(forks-snapshot['forks'], 'weekly snapshots')
            if forks is not None and snapshot.get('forks') is not None else Metric())
        metrics['fork_growth'] = growth(forks, snapshot.get('forks'), 'weekly snapshots')
    total = candidate.metric('discussion_total').value
    old_total = snapshot.get('discussion_total')
    discussion_delta = total-old_total if total is not None and old_total is not None else None
    metrics['discussion_delta'] = Metric.known(discussion_delta,'net discussion total change') if discussion_delta is not None else Metric()
    metrics['discussion_growth'] = growth(total, old_total, 'discussion total snapshot growth')
    if candidate.repository.created_at:
        age = (period.end-instant(candidate.repository.created_at)).total_seconds()/86400
        metrics['age_days'] = Metric.known(max(0, age), 'GitHub created_at')
        # Discovery-only priority keeps its original latest-metadata meaning;
        # evaluation T/Star signals continue using the boundary repository.
        stars = (candidate.latest_repository or candidate.repository).stars
        metrics['new_repository_priority'] = Metric.known(
            stars is not None and any(0 <= age <= days and stars >= threshold for days,threshold in rules.NEW_REPO_THRESHOLDS),
            'GitHub created_at and stars; discovery signal only')
    derive_signals(candidate)


def derive_signals(candidate):
    signals = set()
    stars = candidate.repository.stars
    absolute, rate = rules.star_threshold(stars) if stars is not None else (None, None)
    delta, growth_rate = candidate.metric('star_delta').value, candidate.metric('star_growth').value
    if ((absolute is not None and delta is not None and delta >= absolute) or
        (rate is not None and growth_rate is not None and growth_rate >= rate)):
        signals.add('star')
    if candidate.metric('trending').value is True:
        signals.add('trending')
    if (candidate.metric('issues').value or 0) >= rules.ISSUE_SIGNAL_COUNT or (candidate.metric('pulls').value or 0) >= rules.PR_SIGNAL_COUNT:
        signals.add('activity')
    if (candidate.metric('activity_growth').value or 0) >= rules.ACTIVITY_GROWTH_BUCKETS[-2][0]:
        signals.add('activity')
    if (candidate.metric('discussion_growth').value or 0) >= rules.DISCUSSION_SIGNAL_GROWTH:
        signals.add('discussion')
    for event in candidate.events:
        if event.source == 'release' and rules.RELEASE_SCORES.get(event.kind, 0) >= rules.RELEASE_SCORES['feature']:
            signals.add('release')
        if event.source == 'security':
            signals.add('security')
        if event.kind in {'license', 'api', 'policy', 'maintenance_stop', 'shutdown', 'controversy'}:
            signals.add('policy')
    candidate.signals = signals
