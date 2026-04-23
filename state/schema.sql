-- WeiboMonitor SQLite Database Schema
-- Three-table design for state persistence, weibo history, and push logging

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