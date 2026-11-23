-- The read audit (user decision 2026-11-23, wanted for payroll): one row per read of
-- a datamodule's data, by a person or by a key, refusals included.
--
-- No foreign keys: an audit outlives what it names (a deleted key, report or
-- datamodule keeps its reads), so the names are kept beside the ids.
-- It replaces api_key_access_log (keyed data requests only, cascade-deleted with
-- their key): its rows are copied in, then it is dropped.
CREATE TABLE read_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,                 -- ISO UTC, when the request started
    kind TEXT NOT NULL,               -- page, data, run, run-data, excel-key, preview, script, source
    username TEXT,                    -- the signed-in person; NULL for a key or anonymous
    api_key_id INTEGER,
    key_name TEXT,
    token_prefix TEXT,                -- the key's prefix, or of a token that matched no key
    datamodule_id INTEGER,
    datamodule_path TEXT,
    report_id INTEGER,
    report_name TEXT,
    run_uuid TEXT,
    version_uuid TEXT,
    format TEXT,
    query TEXT,                       -- the query string without key=, clipped
    status INTEGER,
    row_count INTEGER,
    bytes_sent INTEGER,
    duration_ms INTEGER,
    remote_addr TEXT,
    user_agent TEXT
);

CREATE INDEX idx_read_log_at ON read_log(at);
CREATE INDEX idx_read_log_datamodule ON read_log(datamodule_id, id);
CREATE INDEX idx_read_log_key ON read_log(api_key_id, id);
CREATE INDEX idx_read_log_user ON read_log(username, id);

INSERT INTO read_log (at, kind, api_key_id, key_name, token_prefix, datamodule_id,
                      datamodule_path, report_id, report_name, format, status, row_count,
                      bytes_sent, duration_ms, remote_addr)
SELECT l.accessed_at, 'data', l.api_key_id, k.name, k.token_prefix,
       COALESCE(r.datamodule_id, k.datamodule_id), d.path, l.report_id, r.name, l.format,
       l.status, l.row_count, l.bytes_sent, l.duration_ms, l.remote_addr
FROM api_key_access_log l
LEFT JOIN api_key k ON k.id = l.api_key_id
LEFT JOIN report r ON r.id = l.report_id
LEFT JOIN datamodule d ON d.id = COALESCE(r.datamodule_id, k.datamodule_id)
ORDER BY l.id;

DROP TABLE api_key_access_log;
