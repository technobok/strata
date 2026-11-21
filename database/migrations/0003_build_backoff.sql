-- Phase B2: serving never builds; failed builds back off.
--
-- After a failed build, automatic rebuilds (stale reads, dependency pulls) are
-- not attempted again before retry_after: min(60s * 2^(failures-1), 1h).
-- Manual and scheduled builds ignore it. A successful build resets both.

ALTER TABLE datamodule ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE datamodule ADD COLUMN retry_after TEXT;
