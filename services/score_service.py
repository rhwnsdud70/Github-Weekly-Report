from config import rules
from models.domain import ScoreResult


def bucket(value, buckets):
    if value is None:
        return None
    return next((score for minimum, score in buckets if value >= minimum), 0)


def star_score(stars, delta):
    if stars is None or delta is None:
        return None
    threshold, _ = rules.star_threshold(stars)
    return bucket(delta / threshold, rules.STAR_BUCKETS) if threshold else None


def growth_score(rate, age_days):
    score = bucket(rate, rules.GROWTH_BUCKETS)
    if score is not None and (age_days is None or age_days <= rules.NEW_REPO_DAYS):
        score = min(score, rules.NEW_REPO_GROWTH_CAP)
    return score


def trending_score(present, rank=None, clearly_top=False):
    if present is None:
        return None
    if not present:
        return 0
    if rank is not None:
        if rank < 1:
            raise ValueError('Trending rank must be positive')
        return next((score for upper, score in rules.TRENDING_BUCKETS if rank <= upper), rules.TRENDING_LOW)
    return rules.TRENDING_TOP if clearly_top else rules.TRENDING_CONFIRMED


def score_candidate(candidate):
    value = lambda key: candidate.metric(key).value
    absolute = bucket(value('activity'), rules.ACTIVITY_BUCKETS)
    increase = bucket(value('activity_growth'), rules.ACTIVITY_GROWTH_BUCKETS)
    community = value('community_level')
    if community is not None and not 0 <= community <= 5:
        raise ValueError('Community level must be 0..5')
    if community is None:
        rates = [value(k) for k in ('fork_growth', 'discussion_growth') if value(k) is not None]
        community = bucket(max(rates), rules.COMMUNITY_GROWTH_BUCKETS) if rates else None
    releases = [rules.RELEASE_SCORES[e.kind] for e in candidate.events
                if e.source == 'release' and e.kind in rules.RELEASE_SCORES]
    specials = [rules.SPECIAL_SCORES[e.kind] for e in candidate.events if e.kind in rules.SPECIAL_SCORES]
    components = {
        'star_delta': star_score(candidate.repository.stars, value('star_delta')),
        'growth_rate': growth_score(value('star_growth'), value('age_days')),
        'trending': trending_score(value('trending'), value('trending_rank'), value('trending_clearly_top')),
        'activity': (sum(v for v in (absolute, increase) if v is not None)
                     if absolute is not None or increase is not None else None),
        'community': community,
        'release': max(releases) if releases else (0 if value('releases_checked') else None),
        'special': max(specials) if specials else (0 if value('security_checked') else None),
    }
    candidate.score = ScoreResult(components, {'activity_absolute': absolute, 'activity_growth': increase,
        'unknown_components': [name for name, score in components.items() if score is None]})
    return candidate.score
