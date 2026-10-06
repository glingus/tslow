-- M8: Italian enum values -> English (sampler_state, resource, incident_type, severity,
-- proposed_action, status). SQLite can't alter a CHECK constraint, so the two tables that carry
-- one are rebuilt (the documented 12-step recipe, foreign keys off while swapping).

PRAGMA foreign_keys=OFF;
BEGIN;

CREATE TABLE metrics_raw_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    sampler_state TEXT NOT NULL CHECK (sampler_state IN ('idle', 'watching', 'investigating')),
    period_ms INTEGER NOT NULL,
    cpu_percent REAL,
    cpu_queue_per_core REAL,
    cpu_perf_percent REAL,
    cpu_perf_limit_percent REAL,
    ram_percent REAL,
    ram_available_mb REAL,
    ram_commit_percent REAL,
    page_reads_sec REAL,
    disk_read_bytes_sec REAL,
    disk_write_bytes_sec REAL,
    disk_latency_ms REAL,
    disk_queue_length REAL,
    disk_free_gb REAL,
    net_rx_bytes_sec REAL,
    net_tx_bytes_sec REAL,
    gpu_percent REAL,
    battery_percent REAL
);
INSERT INTO metrics_raw_new
SELECT id, ts_ms,
       CASE sampler_state WHEN 'calmo' THEN 'idle' WHEN 'attenzione' THEN 'watching' WHEN 'indagine' THEN 'investigating' ELSE sampler_state END,
       period_ms, cpu_percent, cpu_queue_per_core, cpu_perf_percent, cpu_perf_limit_percent, ram_percent,
       ram_available_mb, ram_commit_percent, page_reads_sec, disk_read_bytes_sec, disk_write_bytes_sec,
       disk_latency_ms, disk_queue_length, disk_free_gb, net_rx_bytes_sec, net_tx_bytes_sec, gpu_percent,
       battery_percent
FROM metrics_raw;
DELETE FROM sqlite_sequence WHERE name = 'metrics_raw_new';
INSERT INTO sqlite_sequence (name, seq) SELECT 'metrics_raw_new', seq FROM sqlite_sequence WHERE name = 'metrics_raw';
DROP TABLE metrics_raw;
ALTER TABLE metrics_raw_new RENAME TO metrics_raw;
CREATE INDEX idx_metrics_raw_ts ON metrics_raw (ts_ms);

CREATE TABLE incidents_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at_ms INTEGER NOT NULL,
    closed_at_ms INTEGER,
    resource TEXT NOT NULL CHECK (resource IN ('cpu', 'ram', 'disk', 'network', 'gpu', 'system', 'app')),
    incident_type TEXT NOT NULL CHECK (incident_type IN ('anomaly', 'critical', 'leak', 'hung', 'throttling')),
    severity TEXT NOT NULL CHECK (severity IN ('anomaly', 'critical')),
    group_key TEXT,
    exe_path TEXT,
    culprit_pids_json TEXT,
    quota_percent REAL,
    protection_level INTEGER,
    proposed_action TEXT NOT NULL CHECK (proposed_action IN ('soft', 'hard', 'none')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'expired', 'ignored')),
    detail_json TEXT
);
INSERT INTO incidents_new
SELECT id, opened_at_ms, closed_at_ms,
       CASE resource WHEN 'disco' THEN 'disk' WHEN 'rete' THEN 'network' WHEN 'sistema' THEN 'system' ELSE resource END,
       CASE incident_type WHEN 'anomalia' THEN 'anomaly' WHEN 'critico' THEN 'critical' ELSE incident_type END,
       CASE severity WHEN 'anomalia' THEN 'anomaly' WHEN 'critico' THEN 'critical' ELSE severity END,
       group_key, exe_path, culprit_pids_json, quota_percent, protection_level,
       CASE proposed_action WHEN 'nessuna' THEN 'none' ELSE proposed_action END,
       CASE status WHEN 'aperto' THEN 'open' WHEN 'risolto' THEN 'resolved' WHEN 'scaduto' THEN 'expired' WHEN 'ignorato' THEN 'ignored' ELSE status END,
       detail_json
FROM incidents;
DELETE FROM sqlite_sequence WHERE name = 'incidents_new';
INSERT INTO sqlite_sequence (name, seq) SELECT 'incidents_new', seq FROM sqlite_sequence WHERE name = 'incidents';
DROP TABLE incidents;
ALTER TABLE incidents_new RENAME TO incidents;
CREATE INDEX idx_incidents_status_opened ON incidents (status, opened_at_ms);
CREATE INDEX idx_incidents_group_resource ON incidents (group_key, resource);

UPDATE monitor_health
SET sampler_state = CASE sampler_state WHEN 'calmo' THEN 'idle' WHEN 'attenzione' THEN 'watching' WHEN 'indagine' THEN 'investigating' ELSE sampler_state END;

COMMIT;
PRAGMA foreign_keys=ON;
