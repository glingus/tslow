-- M2: baseline EWMA, incidenti, notifiche.

CREATE TABLE baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL,
    hour_of_day INTEGER NOT NULL CHECK (hour_of_day BETWEEN 0 AND 23),
    ewma_mean REAL NOT NULL,
    ewma_var REAL NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (metric, hour_of_day)
);

CREATE TABLE incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at_ms INTEGER NOT NULL,
    closed_at_ms INTEGER,
    resource TEXT NOT NULL CHECK (resource IN ('cpu', 'ram', 'disco', 'rete', 'gpu', 'sistema', 'app')),
    incident_type TEXT NOT NULL CHECK (incident_type IN ('anomalia', 'critico', 'leak', 'hung', 'throttling')),
    severity TEXT NOT NULL CHECK (severity IN ('anomalia', 'critico')),
    group_key TEXT,
    exe_path TEXT,
    culprit_pids_json TEXT,
    quota_percent REAL,
    protection_level INTEGER,
    proposed_action TEXT NOT NULL CHECK (proposed_action IN ('soft', 'hard', 'nessuna')),
    status TEXT NOT NULL DEFAULT 'aperto' CHECK (status IN ('aperto', 'risolto', 'scaduto', 'ignorato')),
    detail_json TEXT
);
CREATE INDEX idx_incidents_status_opened ON incidents (status, opened_at_ms);
CREATE INDEX idx_incidents_group_resource ON incidents (group_key, resource);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents (id),
    sent_at_ms INTEGER NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('toast', 'plyer', 'log')),
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    error TEXT
);
CREATE INDEX idx_notifications_incident ON notifications (incident_id);
CREATE INDEX idx_notifications_sent_at ON notifications (sent_at_ms);
