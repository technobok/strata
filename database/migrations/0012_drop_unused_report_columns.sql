-- Two report columns nothing uses (X6, user decision 2026-11-23).
--
-- default_format: the report form edited it, nothing read it: a report's format is
-- its URL's extension (/r/<id>.xlsx).
-- ttl_seconds: a per-report freshness override no form or caller ever set, so every
-- report used its datamodule's TTL; freshness is per datamodule.
--
-- DROP COLUMN needs no table rebuild here: neither column is indexed or named by
-- another constraint (default_format's CHECK is its own).
ALTER TABLE report DROP COLUMN default_format;
ALTER TABLE report DROP COLUMN ttl_seconds;
