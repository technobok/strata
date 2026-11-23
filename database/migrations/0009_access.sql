-- AC1: who may open and edit each datamodule.
--
-- owners_json   JSON list of principals (usernames, @groups); owners edit the
--               datamodule, its reports, schedules, keys and access.
-- viewers_json  JSON list of principals (usernames, @groups, * = everyone signed
--               in); viewers open its reports and runs. Owners also view.
--               NULL (either) = follow access.default_owners / access.default_viewers.
--               '[]' owners = no owners besides the admins.
-- access_lock   why the datamodule serves nothing: a build found that an owner may
--               no longer read a parent or use a connection (AC4). NULL = not
--               locked; the next successful build clears it.
ALTER TABLE datamodule ADD COLUMN owners_json TEXT;
ALTER TABLE datamodule ADD COLUMN viewers_json TEXT;
ALTER TABLE datamodule ADD COLUMN access_lock TEXT;

-- Datamodules made before access control stay as they were (user decision
-- 2026-11-23): owned by the admins only, viewed by everyone (the default).
UPDATE datamodule SET owners_json = '[]';
