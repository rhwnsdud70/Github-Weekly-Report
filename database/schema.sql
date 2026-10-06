PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS repositories (
    identity TEXT PRIMARY KEY, github_id INTEGER UNIQUE, full_name TEXT NOT NULL,
    canonical_url TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_runs (
    run_id TEXT PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
    mode TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, error TEXT, report TEXT
);
CREATE TABLE IF NOT EXISTS weekly_snapshots (
    identity TEXT NOT NULL REFERENCES repositories(identity), period_end TEXT NOT NULL,
    measured_at TEXT NOT NULL, stars INTEGER, forks INTEGER, data TEXT NOT NULL,
    PRIMARY KEY(identity, period_end)
);
CREATE TABLE IF NOT EXISTS weekly_metrics (
    identity TEXT NOT NULL REFERENCES repositories(identity), period_end TEXT NOT NULL,
    star_delta INTEGER, star_delta_status TEXT NOT NULL CHECK(star_delta_status IN
    ('confirmed','unknown','unavailable','not_applicable')),
    metrics TEXT NOT NULL, score TEXT NOT NULL,
    CHECK((star_delta_status = 'confirmed' AND star_delta IS NOT NULL) OR
          (star_delta_status != 'confirmed' AND star_delta IS NULL)),
    PRIMARY KEY(identity, period_end)
);
CREATE TABLE IF NOT EXISTS report_history (
    identity TEXT NOT NULL REFERENCES repositories(identity), period_end TEXT NOT NULL,
    rank INTEGER NOT NULL, score INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(identity, period_end)
);
CREATE TABLE IF NOT EXISTS watchlist (
    identity TEXT NOT NULL REFERENCES repositories(identity), period_end TEXT NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY(identity, period_end)
);
CREATE TABLE IF NOT EXISTS telegram_logs (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES automation_runs(run_id),
    message_type TEXT NOT NULL, top_rank INTEGER, part_number INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL, status TEXT NOT NULL, error_message TEXT,
    sent_time TEXT NOT NULL
);
