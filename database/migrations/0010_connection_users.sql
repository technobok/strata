-- AC3: who may use each connection in a build.
--
-- users_json  JSON list of principals (usernames, @groups, * = everyone signed in)
--             who may bind the connection to a datamodule they own. Every owner of
--             a datamodule must be on the list of each connection bound to it,
--             checked when binding, when its owners change, and at every build.
--             NULL = the Strata admins only (so nothing widens by accident).
ALTER TABLE connection ADD COLUMN users_json TEXT;
