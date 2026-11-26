-- WeiboMonitor SQLite Database Schema
-- Existing account, history and push tables remain available to older clients.

-- Account State Table (replaces state.yaml)
CREATE TABLE IF NOT EXISTS account_state (
    weiboid TEXT PRIMARY KEY,
    latest_id TEXT NOT NULL,
    screen_name TEXT,
    last_check_time TIMESTAMP,
    last_update_time TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Weibo History Table (permanent storage of all weibos)
CREATE TABLE IF NOT EXISTS weibo_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weiboid TEXT NOT NULL,
    weibo_id TEXT NOT NULL,
    text TEXT NOT NULL,
    screen_name TEXT NOT NULL,
    source TEXT,
    region_name TEXT,
    created_time TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(weiboid, weibo_id),
    FOREIGN KEY (weiboid) REFERENCES account_state(weiboid)
);

-- Push Log Table (per-channel push results)
CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weiboid TEXT NOT NULL,
    weibo_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (weiboid) REFERENCES account_state(weiboid)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_weibo_history_weiboid ON weibo_history(weiboid);
CREATE INDEX IF NOT EXISTS idx_weibo_history_fetched_at ON weibo_history(fetched_at);
CREATE INDEX IF NOT EXISTS idx_push_log_weiboid ON push_log(weiboid);
CREATE INDEX IF NOT EXISTS idx_push_log_pushed_at ON push_log(pushed_at);
CREATE INDEX IF NOT EXISTS idx_push_log_status ON push_log(status);

CREATE TABLE IF NOT EXISTS event_snapshots (
    account_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    revision INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY (account_id, stream)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    delivered_at TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    failed_at TEXT,
    delivery_token TEXT,
    lease_until TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_events ON events(account_id) WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS event_push_log (
    event_id TEXT NOT NULL REFERENCES events(event_id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    pushed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
