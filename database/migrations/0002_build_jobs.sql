-- Phase B1: builds leave the web request.
--
-- build_job      requested builds; workers claim them (one running per datamodule).
-- worker_process one heartbeat row per running strata-worker.
-- datamodule.build_queue  which worker queue builds this datamodule.
--
-- The cross-process single-flight lease uses the existing datamodule columns
-- build_token / build_lease_until (unused until now).

CREATE TABLE IF NOT EXISTS build_job (
    id INTEGER PRIMARY KEY,
    datamodule_id INTEGER NOT NULL REFERENCES datamodule(id) ON DELETE CASCADE,
    queue TEXT NOT NULL DEFAULT 'default',
    reason TEXT NOT NULL,                     -- manual:<user> | schedule:<name> | stale-read | upstream:<path>
    force INTEGER NOT NULL DEFAULT 0,         -- 1 = rebuild even if the live version is fresh
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'skipped')),
    requested_at TEXT NOT NULL,
    run_after TEXT NOT NULL,                  -- not claimed before this time
    started_at TEXT,
    finished_at TEXT,
    worker_id TEXT,                           -- worker_process.id that claimed it
    version_id INTEGER REFERENCES datamodule_version(id) ON DELETE SET NULL,
    error TEXT
);

-- Coalescing: at most one queued job per datamodule.
CREATE UNIQUE INDEX IF NOT EXISTS idx_build_job_one_queued
    ON build_job(datamodule_id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_build_job_claim ON build_job(status, queue, run_after);
CREATE INDEX IF NOT EXISTS idx_build_job_dm ON build_job(datamodule_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS worker_process (
    id TEXT PRIMARY KEY,                      -- <hostname>:<pid>:<random>
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    queues TEXT NOT NULL,                     -- comma-separated
    concurrency INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

ALTER TABLE datamodule ADD COLUMN build_queue TEXT NOT NULL DEFAULT 'default';
