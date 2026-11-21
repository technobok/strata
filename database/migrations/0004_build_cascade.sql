-- "Build with all dependencies": a forced build that also force-rebuilds every
-- dependency (transitively, each datamodule once), ignoring their TTLs.

ALTER TABLE build_job ADD COLUMN cascade INTEGER NOT NULL DEFAULT 0;
