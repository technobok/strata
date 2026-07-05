-- Strata baseline schema (datamodule-centric).
--
-- This is the v1 baseline for the "datamodule as core" architecture. There is
-- no backward compatibility with the old report-centric schema. Applied by the
-- forward-only migration runner in db.py; the runner records it in
-- schema_migrations and never re-applies it. During early development (before
-- any real data exists) this file may be edited and the dev DB re-created with
-- `make clean && make init-db`; once datamodules hold real versioned data,
-- changes must ship as new NNNN_*.sql migrations instead.

-- ---------------------------------------------------------------------------
-- Key/value + config (reused from the old schema)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_setting (
    key TEXT PRIMARY KEY,
    value TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS db_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------------------
-- External connections (reused unchanged; consumed by build scripts via
-- datamodule_connection + connection_service.attach_into)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS connection (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    driver TEXT NOT NULL,
    params_encrypted TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    modified_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Datamodule: the core object. A persistent, versioned DuckDB database built
-- by a build script. "live" is a pointer (live_version_id) to an immutable
-- snapshot under versions/ on disk -- never a mutated-in-place file.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS datamodule (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,               -- storage key (2-char fan-out) + URL key
    path TEXT NOT NULL UNIQUE,               -- 'finance/payroll/monthly' (human hierarchy; metadata only)
    description TEXT NOT NULL DEFAULT '',
    build_script TEXT NOT NULL DEFAULT '',
    ttl_seconds INTEGER,                      -- NULL = never auto-expire; 0 = always stale (ephemeral)
    valid_until TEXT,                         -- optional fixed expiry (ISO-8601 UTC)
    retention_kind TEXT NOT NULL DEFAULT 'count' CHECK (retention_kind IN ('count', 'age')),
    retention_count INTEGER NOT NULL DEFAULT 5,
    retention_age_seconds INTEGER,
    storage_version TEXT,                     -- pinned DuckDB storage format (e.g. 'v1.2.0')
    live_version_id INTEGER REFERENCES datamodule_version(id) ON DELETE SET NULL,
    build_status TEXT NOT NULL DEFAULT 'never'
        CHECK (build_status IN ('never', 'building', 'ready', 'failed')),
    build_token TEXT,                         -- single-flight lease owner
    build_lease_until TEXT,                   -- lease expiry (reap crashed builders)
    last_build_error TEXT,
    created_by TEXT NOT NULL,
    modified_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datamodule_version (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,                -- == versions/<uuid>.duckdb filename
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed', 'superseded')),
    build_started_at TEXT,
    build_ended_at TEXT,
    duration_ms INTEGER,
    triggered_by TEXT NOT NULL,              -- username | 'schedule:<name>' | 'ttl-pull' | 'dependency:<dm>'
    build_script_snapshot TEXT,              -- copy of the build script that ran
    byte_size INTEGER,
    row_summary_json TEXT,
    storage_version TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dmver_dm_status_ended
    ON datamodule_version(datamodule_id, status, build_ended_at DESC);

-- ---------------------------------------------------------------------------
-- Reports = DuckDB macros layered on a datamodule.
--   kind='query' -> parameterised data report (author SELECT body)
--   kind='view'  -> formatting report (system-generated body; see format_*)
-- Macro bodies live here (SQLite is source of truth); serving registers them
-- as TEMP macros over the read-only live version file.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS report (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,                -- URL key
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    name TEXT NOT NULL,                       -- human label
    macro_name TEXT NOT NULL,                 -- sanitised DuckDB identifier
    kind TEXT NOT NULL CHECK (kind IN ('query', 'view')),
    description TEXT NOT NULL DEFAULT '',
    body_sql TEXT NOT NULL DEFAULT '',
    ttl_seconds INTEGER,                      -- per-report override of datamodule TTL
    default_format TEXT NOT NULL DEFAULT 'html'
        CHECK (default_format IN ('html', 'parquet', 'xlsx', 'csv', 'json')),
    created_by TEXT NOT NULL,
    modified_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    UNIQUE (datamodule_id, macro_name),
    UNIQUE (datamodule_id, name)
);

CREATE INDEX IF NOT EXISTS idx_report_datamodule ON report(datamodule_id);

CREATE TABLE IF NOT EXISTS report_arg (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    name TEXT NOT NULL,                       -- label-safe; becomes the macro parameter
    data_type TEXT NOT NULL
        CHECK (data_type IN ('text', 'integer', 'number', 'decimal', 'date', 'time', 'boolean')),
    widget TEXT NOT NULL DEFAULT 'input'
        CHECK (widget IN ('input', 'select', 'multiselect')),  -- multiselect => array (LIST) arg
    default_value TEXT,
    required INTEGER NOT NULL DEFAULT 1,
    display_order INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    constraint_query TEXT,                    -- SELECT returning (name, description) to populate a select
    constraint_json TEXT,                     -- optional min/max/len/range extras
    UNIQUE (report_id, name)
);

-- ---------------------------------------------------------------------------
-- Formatting-report ("view") builder state (Phase 7). Tables exist in the
-- baseline; unused until the view builder ships.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS format_view (
    report_id INTEGER PRIMARY KEY REFERENCES report(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('report', 'relation')),
    source_report_id INTEGER REFERENCES report(id) ON DELETE SET NULL,
    source_relation TEXT,
    table_class TEXT
);

CREATE TABLE IF NOT EXISTS format_column (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL DEFAULT 0,
    output_name TEXT NOT NULL,
    source_column TEXT,
    format_kind TEXT NOT NULL
        CHECK (format_kind IN ('number', 'date', 'text', 'currency', 'link', 'custom', 'none')),
    expression_sql TEXT,                      -- author-controlled inner value expression (system escapes)
    decimals INTEGER,
    thousands INTEGER,
    date_format TEXT,
    align TEXT CHECK (align IN ('left', 'right', 'center')),
    css_class TEXT,
    link_report_id INTEGER REFERENCES report(id) ON DELETE SET NULL,
    link_args_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_format_column_report ON format_column(report_id);

-- ---------------------------------------------------------------------------
-- API keys (generalise the old api_link): named, revocable, hashed tokens for
-- unauthenticated (Gatekeeper-bypass) access from Excel/PowerQuery. (Phase 6)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_key (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,                -- public management id
    token_hash TEXT NOT NULL UNIQUE,         -- sha256 of the bearer token (never store plaintext)
    token_prefix TEXT NOT NULL,              -- first few chars for display
    name TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('datamodule', 'report')),
    datamodule_id INTEGER REFERENCES datamodule(id) ON DELETE CASCADE,
    report_id INTEGER REFERENCES report(id) ON DELETE CASCADE,
    fixed_args_json TEXT,
    parameterised_args_json TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_key_access_log (
    id INTEGER PRIMARY KEY,
    api_key_id INTEGER NOT NULL REFERENCES api_key(id) ON DELETE CASCADE,
    report_id INTEGER,
    accessed_at TEXT NOT NULL,
    remote_addr TEXT,
    format TEXT,
    row_count INTEGER,
    bytes_sent INTEGER,
    duration_ms INTEGER,
    status INTEGER
);

CREATE INDEX IF NOT EXISTS idx_apikey_log_key ON api_key_access_log(api_key_id, accessed_at DESC);

-- ---------------------------------------------------------------------------
-- Datamodule inputs: uploaded source files + dependencies on other datamodules
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS source_file (
    id INTEGER PRIMARY KEY,
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,                   -- original name (as referenced in build scripts)
    stored_name TEXT NOT NULL,               -- name on disk under sources/
    byte_size INTEGER,
    content_type TEXT,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    UNIQUE (datamodule_id, filename)
);

CREATE TABLE IF NOT EXISTS dependency (
    id INTEGER PRIMARY KEY,
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    local_name TEXT NOT NULL,                -- ATTACH alias exposed to the build script
    UNIQUE (datamodule_id, local_name),
    CHECK (depends_on_id <> datamodule_id)
);

CREATE TABLE IF NOT EXISTS datamodule_connection (
    id INTEGER PRIMARY KEY,
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    connection_id INTEGER NOT NULL REFERENCES connection(id) ON DELETE CASCADE,
    local_name TEXT NOT NULL,
    UNIQUE (datamodule_id, local_name)
);

-- ---------------------------------------------------------------------------
-- Tags + FTS over datamodules
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tag (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datamodule_tag (
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    PRIMARY KEY (datamodule_id, tag_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS datamodule_fts USING fts5(
    path, description, tags_text,
    tokenize='porter unicode61'
);

-- ---------------------------------------------------------------------------
-- Scheduling (adapted: targets a datamodule; action = pre-warm rebuild or email)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    report_id INTEGER REFERENCES report(id) ON DELETE CASCADE,  -- required for action='email'
    name TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'rebuild' CHECK (action IN ('rebuild', 'email')),
    enabled INTEGER NOT NULL DEFAULT 1,
    schedule_definition TEXT NOT NULL,
    parameters_json TEXT,
    recipients_json TEXT NOT NULL DEFAULT '[]',
    max_inline_rows INTEGER NOT NULL DEFAULT 100,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT
);
