-- Phase C1: checks and run snapshots.
--
-- A check is a report with exception settings: a run that finds rows is an
-- exception. A gate check also runs inside every build of its datamodule,
-- before the swap: rows fail the build and the previous version stays live.
CREATE TABLE report_check (
    report_id INTEGER PRIMARY KEY REFERENCES report(id) ON DELETE CASCADE,
    severity TEXT NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'error')),
    gate INTEGER NOT NULL DEFAULT 0
);

-- One run of a report or check. Its rows are saved as a Parquet snapshot at
-- DATA_ROOT/<uuid[:2]>/<datamodule uuid>/<snapshot_path>; the row is written
-- when the run ends. Runs older than runs.retention_days are swept with their
-- files. version_id goes NULL when retention prunes the version; data_as_of
-- keeps its build time.
CREATE TABLE report_run (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    report_id INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    schedule_id INTEGER REFERENCES schedule(id) ON DELETE SET NULL,
    version_id INTEGER REFERENCES datamodule_version(id) ON DELETE SET NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'error')),
    row_count INTEGER,
    data_as_of TEXT,
    data_note TEXT,                 -- e.g. why the data may be out of date
    error TEXT,                     -- status 'error': why the query failed
    triggered_by TEXT NOT NULL,     -- username | schedule:<name>
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    notified_at TEXT,
    snapshot_path TEXT              -- runs/<uuid>.parquet; NULL for an error
);

CREATE INDEX idx_report_run_report ON report_run(report_id, started_at);
CREATE INDEX idx_report_run_started ON report_run(started_at);
