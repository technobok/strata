-- Phase C2: report and check schedules.
--
-- A due report schedule queues a 'run' job in build_job: the worker runs the
-- report (refreshing a stale datamodule first, in the same thread) and mails
-- the run to the schedule's recipients. Run jobs share the "one running job
-- per datamodule" rule with builds, so a run never races a build of its own
-- datamodule; they do not coalesce (each due time is its own run).
ALTER TABLE build_job ADD COLUMN kind TEXT NOT NULL DEFAULT 'build' CHECK (kind IN ('build', 'run'));
ALTER TABLE build_job ADD COLUMN report_id INTEGER REFERENCES report(id) ON DELETE CASCADE;
ALTER TABLE build_job ADD COLUMN schedule_id INTEGER REFERENCES schedule(id) ON DELETE SET NULL;
ALTER TABLE build_job ADD COLUMN run_id INTEGER REFERENCES report_run(id) ON DELETE SET NULL;

-- Coalescing now covers builds only.
DROP INDEX IF EXISTS idx_build_job_one_queued;
CREATE UNIQUE INDEX idx_build_job_one_queued
    ON build_job(datamodule_id) WHERE status = 'queued' AND kind = 'build';

-- What happened to the run's mail: who got it, who could not be resolved, or
-- why it was not sent.
ALTER TABLE report_run ADD COLUMN notify_note TEXT;
