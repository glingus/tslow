-- M3: apprendimento dalle scelte dell'utente, decisioni e log delle azioni.

CREATE TABLE user_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_identity TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    consecutive_denies INTEGER NOT NULL DEFAULT 0,
    allow_once_count INTEGER NOT NULL DEFAULT 0,
    max_auto_action TEXT CHECK (max_auto_action IN ('soft')),
    source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'ignore_learned')),
    expires_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_user_rules_identity ON user_rules (app_identity);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents (id),
    created_at_ms INTEGER NOT NULL,
    choice TEXT NOT NULL CHECK (choice IN ('deny', 'allow_once', 'allow_always')),
    source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'auto')),
    execution_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        execution_status IN ('pending', 'ok', 'access_denied', 'no_such_process', 'identity_mismatch', 'protected_refused', 'dry_run')
    ),
    executed_at_ms INTEGER
);
CREATE INDEX idx_decisions_incident ON decisions (incident_id);
CREATE INDEX idx_decisions_execution_status ON decisions (execution_status);

CREATE TABLE actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER REFERENCES decisions (id),
    incident_id INTEGER NOT NULL REFERENCES incidents (id),
    executed_at_ms INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('soft_priority', 'soft_ionice', 'hard_close', 'hard_kill', 'undo')),
    pid INTEGER NOT NULL,
    previous_priority INTEGER,
    previous_io_priority INTEGER,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('ok', 'access_denied', 'no_such_process', 'identity_mismatch', 'protected_refused', 'dry_run')
    )
);
CREATE INDEX idx_actions_log_decision ON actions_log (decision_id);
CREATE INDEX idx_actions_log_incident ON actions_log (incident_id);
