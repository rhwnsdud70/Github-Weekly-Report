import pytest
from config import rules
from models.domain import Candidate, Repository, Metric, Event
from services.score_service import bucket, star_score, growth_score, trending_score, score_candidate


@pytest.mark.parametrize('stars,threshold', [(100,50),(499,50),(500,100),(1999,100),(2000,300),(9999,300),(10000,700),(49999,700),(50000,1500)])
@pytest.mark.parametrize('multiple,expected', [(0,0),(.99,0),(1,10),(1.5,15),(2,20),(3,25),(4,25)])
def test_star_buckets(stars, threshold, multiple, expected):
    assert star_score(stars, threshold*multiple) == expected


@pytest.mark.parametrize('rate,expected', [(-1,0),(0,0),(4.99,0),(5,4),(9.99,4),(10,8),(19.99,8),(20,12),(49.99,12),(50,16),(99.99,16),(100,20)])
def test_growth_buckets(rate, expected):
    assert growth_score(rate, 31) == expected


@pytest.mark.parametrize('rank,expected', [(1,15),(5,15),(6,12),(15,12),(16,8),(30,8),(31,4)])
def test_trending_buckets(rank, expected):
    assert trending_score(True, rank) == expected


@pytest.mark.parametrize('value,expected', [(0,0),(4,0),(5,2),(9,2),(10,4),(29,4),(30,6),(99,6),(100,7)])
def test_activity_buckets(value, expected):
    assert bucket(value, rules.ACTIVITY_BUCKETS) == expected


@pytest.mark.parametrize('value,expected', [(0,0),(24.99,0),(25,2),(49.99,2),(50,4),(99.99,4),(100,6),(199.99,6),(200,8)])
def test_activity_growth_buckets(value, expected):
    assert bucket(value, rules.ACTIVITY_GROWTH_BUCKETS) == expected


@pytest.mark.parametrize('kind,expected', list(rules.RELEASE_SCORES.items()))
def test_release(kind, expected):
    c = Candidate(Repository(1, 'a/b', 'url'), events=[Event(kind, kind, 'url', 'date', 'release')])
    assert score_candidate(c).components['release'] == expected


@pytest.mark.parametrize('kind,expected', list(rules.SPECIAL_SCORES.items()))
def test_special(kind, expected):
    c = Candidate(Repository(1, 'a/b', 'url'), events=[Event(kind, kind, 'url', 'date', 'security')])
    assert score_candidate(c).components['special'] == expected


def test_cap_unknown_and_total():
    assert growth_score(100, 30) == 16
    assert growth_score(100, 31) == 20
    assert growth_score(None, 5) is None
    assert trending_score(None) is None
    assert trending_score(False) == 0
    assert trending_score(True) == 10
    assert trending_score(True, clearly_top=True) == 15
    c = Candidate(Repository(1, 'a/b', 'url', stars=1000))
    assert score_candidate(c).components['star_delta'] is None
    c.metrics = {k: Metric.known(v) for k,v in {'star_delta':300,'star_growth':100,'age_days':31,
        'trending':True,'trending_rank':1,'activity':100,'activity_growth':200,'community_level':5}.items()}
    c.events = [Event('ecosystem','release','r','date','release'), Event('critical','security','s','date','security')]
    assert score_candidate(c).total == 100
