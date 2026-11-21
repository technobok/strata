-- Phase C3: who hears when a datamodule's data stops refreshing.
--
-- alert_recipients     JSON list of usernames / @groups / email addresses;
--                      NULL = the Strata admins (@<auth.admin_group>).
-- stale_alert_seconds  alert once when the last successful build is older than
--                      this; NULL = no stale alert.
-- failure_alerted_at   set when "build failing" was mailed; a later success
--                      mails "recovered" and clears it.
-- stale_alerted_at     set when the stale alert was mailed; a success clears it.
ALTER TABLE datamodule ADD COLUMN alert_recipients TEXT;
ALTER TABLE datamodule ADD COLUMN stale_alert_seconds INTEGER;
ALTER TABLE datamodule ADD COLUMN failure_alerted_at TEXT;
ALTER TABLE datamodule ADD COLUMN stale_alerted_at TEXT;
