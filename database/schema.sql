-- Strata database schema

CREATE TABLE IF NOT EXISTS report (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sql_template TEXT NOT NULL,
    created_by TEXT NOT NULL,
    modified_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_parameter (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    param_type TEXT NOT NULL CHECK (param_type IN ('structural', 'value')),
    data_type TEXT NOT NULL CHECK (data_type IN ('string', 'integer', 'float', 'decimal', 'date', 'boolean')),
    default_value TEXT,
    description TEXT NOT NULL DEFAULT '',
    display_order INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 1,
    UNIQUE(report_id, name)
);

CREATE TABLE IF NOT EXISTS tag (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_tag (
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    PRIMARY KEY (report_id, tag_id)
);

CREATE TABLE IF NOT EXISTS report_access (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    access_type TEXT NOT NULL CHECK (access_type IN ('user', 'group')),
    access_target TEXT NOT NULL,
    permission TEXT NOT NULL CHECK (permission IN ('run', 'edit')),
    UNIQUE(report_id, access_type, access_target, permission)
);

CREATE TABLE IF NOT EXISTS report_run (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    parameters_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    row_count INTEGER,
    column_info_json TEXT,
    result_hash TEXT,
    error_message TEXT,
    run_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    schedule_definition TEXT NOT NULL,
    parameters_json TEXT,
    recipients_json TEXT NOT NULL,
    max_inline_rows INTEGER NOT NULL DEFAULT 100,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT
);

CREATE TABLE IF NOT EXISTS api_link (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    fixed_parameters_json TEXT,
    parameterised_params_json TEXT,
    created_by TEXT NOT NULL,
    expires_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS report_fts USING fts5(
    name, description, tags_text,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS app_setting (key TEXT PRIMARY KEY, value TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS db_metadata (key TEXT PRIMARY KEY, value TEXT);
