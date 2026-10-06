-- M1: tabelle di base per campionamento, rollup e salute del monitor.

CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE system_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at_ms INTEGER NOT NULL,
    cpu_name TEXT,
    cpu_cores_physical INTEGER,
    cpu_cores_logical INTEGER,
    ram_total_mb REAL,
    gpu_names TEXT,
    disk_model TEXT,
    os_version TEXT,
    os_language TEXT
);

CREATE TABLE metrics_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    sampler_state TEXT NOT NULL CHECK (sampler_state IN ('calmo', 'attenzione', 'indagine')),
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
CREATE INDEX idx_metrics_raw_ts ON metrics_raw (ts_ms);

CREATE TABLE metrics_1m (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    minute_ts INTEGER NOT NULL UNIQUE,
    cpu_percent_avg REAL,
    cpu_percent_max REAL,
    ram_percent_avg REAL,
    ram_percent_max REAL,
    disk_latency_ms_avg REAL,
    disk_latency_ms_max REAL,
    net_rx_bytes_sec_avg REAL,
    net_tx_bytes_sec_avg REAL,
    gpu_percent_avg REAL,
    gpu_percent_max REAL,
    sample_count INTEGER NOT NULL
);
CREATE INDEX idx_metrics_1m_ts ON metrics_1m (minute_ts);

CREATE TABLE metrics_1h (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hour_ts INTEGER NOT NULL UNIQUE,
    cpu_percent_avg REAL,
    cpu_percent_max REAL,
    ram_percent_avg REAL,
    ram_percent_max REAL,
    disk_latency_ms_avg REAL,
    disk_latency_ms_max REAL,
    net_rx_bytes_sec_avg REAL,
    net_tx_bytes_sec_avg REAL,
    gpu_percent_avg REAL,
    gpu_percent_max REAL,
    sample_count INTEGER NOT NULL
);
CREATE INDEX idx_metrics_1h_ts ON metrics_1h (hour_ts);

CREATE TABLE process_minutes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    minute_ts INTEGER NOT NULL,
    group_key TEXT NOT NULL,
    display_name TEXT,
    cpu_percent_avg REAL,
    cpu_percent_max REAL,
    ram_private_mb_max REAL,
    io_bytes_sec REAL,
    gpu_percent REAL,
    instance_count INTEGER NOT NULL
);
CREATE INDEX idx_process_minutes_group_minute ON process_minutes (group_key, minute_ts);

CREATE TABLE monitor_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    daemon_cpu_percent REAL,
    daemon_rss_mb REAL,
    sampler_state TEXT,
    period_multiplier REAL
);
CREATE INDEX idx_monitor_health_ts ON monitor_health (ts_ms);
