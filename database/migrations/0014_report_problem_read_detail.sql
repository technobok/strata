-- Why a report does not run on its datamodule's live data, and why a read was refused.
--
-- report.problem: DuckDB's error when the report is described against the live
-- version (a missing table or column, a type error); NULL when it runs, or before
-- the datamodule has data. Set when the report is saved (a save that would set it is
-- refused) and after every successful build, which may break a report that worked.
-- read_log.detail: the text a refused read answered with (e.g. a 400's reason), which
-- Excel does not show.
ALTER TABLE report ADD COLUMN problem TEXT;
ALTER TABLE read_log ADD COLUMN detail TEXT;
