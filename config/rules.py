"""Master specification v1.0 sections 6, 7, 12, 14, 18, 19, 20.

Unspecified policy choices are explicit here and in IMPLEMENTATION_STATUS.
Buckets are inclusive lower bounds; percentages use 100 for 100%.
"""
STAR_THRESHOLDS = ((50000, 1500, 2), (10000, 700, 5), (2000, 300, 10),
                   (500, 100, 20), (100, 50, 25))
STAR_BUCKETS = ((3, 25), (2, 20), (1.5, 15), (1, 10), (0, 0))
GROWTH_BUCKETS = ((100, 20), (50, 16), (20, 12), (10, 8), (5, 4), (0, 0))
ACTIVITY_BUCKETS = ((100, 7), (30, 6), (10, 4), (5, 2), (0, 0))
ACTIVITY_GROWTH_BUCKETS = ((200, 8), (100, 6), (50, 4), (25, 2), (0, 0))
TRENDING_BUCKETS = ((5, 15), (15, 12), (30, 8))
TRENDING_LOW = 4
TRENDING_CONFIRMED = 10
TRENDING_TOP = 15
NEW_REPO_DAYS = 30
NEW_REPO_GROWTH_CAP = 16
NEW_REPO_THRESHOLDS = ((7, 1000), (14, 500), (30, 300))
RELEASE_SCORES = {'none': 0, 'patch': 0, 'minor': 2, 'feature': 4,
                  'performance': 5, 'stable': 7, 'major': 7, 'breaking': 8, 'ecosystem': 10}
SPECIAL_SCORES = {'none': 0, 'policy': 2, 'controversy': 5, 'license': 6,
                  'api': 6, 'maintenance_stop': 7, 'high': 7, 'shutdown': 8,
                  'critical': 10, 'rce': 10, 'supply_chain': 10}
CRITICAL_EVENTS = frozenset({'critical', 'rce', 'supply_chain', 'auth_bypass', 'credential_exposure'})
REPEAT_EVENTS = frozenset({'stable', 'major', 'breaking', 'ecosystem', 'license', 'api',
                         'maintenance_stop', 'shutdown', 'high', 'controversy'} | CRITICAL_EVENTS)
GRADE_BUCKETS = ((85, '핵심 이슈'), (70, '매우 화제'), (55, '주목할 만함'),
                 (40, '관찰 대상'), (0, '낮은 우선순위'))
MIN_SIGNALS = 2
ISSUE_SIGNAL_COUNT = 10
PR_SIGNAL_COUNT = 10
# Quantitative fallback for the qualitative community scale (spec 12.5).
COMMUNITY_GROWTH_BUCKETS = ((200, 5), (100, 4), (50, 3), (25, 2), (0.000001, 1), (0, 0))
CANDIDATE_LIMIT = 40
SNAPSHOT_BOUNDARY_TOLERANCE_SECONDS = 3600
SHORTLIST_LIMIT = 10
TOP_COUNT = 5
SECURITY_PRIORITY_WEIGHTS = {'actively_exploited': 12, 'easy_to_exploit': 5,
    'widespread_impact': 5, 'ecosystem_impact': 5, 'urgent': 5, 'unpatched': 3}
SECURITY_PROJECT_SCALE = ((50000,3),(10000,2),(2000,1))
WATCHLIST_LIMIT = 3
DEEP_LIMITS = {'issues': 3, 'discussions': 2, 'pulls': 2, 'releases': 2}
DEEP_DOCUMENT_CHAR_LIMIT = 6000
DEEP_TOTAL_CHAR_LIMIT = 50000
TELEGRAM_LIMIT = 4096
TELEGRAM_ATTEMPTS = 3
TELEGRAM_DELAY = 1
SEARCH_TOPICS = ('llm', 'ai-agent', 'machine-learning', 'developer-tools', 'database',
                 'web-framework', 'compiler', 'devops', 'security', 'mcp')
RELEVANCE_TERMS = SEARCH_TOPICS + ('ai', 'sdk', 'api', 'runtime', 'ide', 'infrastructure',
    'programming', 'code', 'framework', 'cloud', 'rag', 'artificial-intelligence', 'deep-learning')
EXCLUDED_DESCRIPTION_TERMS = ('homework assignment', 'course assignment', 'learning sample',
                              'meme collection', 'mirror repository')
DISCUSSION_SIGNAL_GROWTH = 25
OFFICIAL_EVENT_PATTERNS = {
    'license': r'^(?:announcement:\s*)?licen[cs]e (?:changed|change to|update to)\b',
    'api': r'^(?:announcement:\s*)?(?:deprecating|removing|sunsetting) (?:the )?api\b',
    'maintenance_stop': r'^(?:announcement:\s*)?(?:maintenance (?:has )?ended|no longer maintained)\b',
    'shutdown': r'^(?:announcement:\s*)?(?:project shutdown|project (?:is )?discontinued)\b',
}


def star_threshold(stars):
    return next(((absolute, growth) for minimum, absolute, growth in STAR_THRESHOLDS
                 if stars >= minimum), (None, None))
