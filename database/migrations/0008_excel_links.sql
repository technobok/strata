-- X3: each report's Excel link.
--
-- A report's Excel key (excel_link = 1) is a report-scoped key that anyone who
-- can open the report copies from its "Use in Excel" box: made the first time
-- someone opens the box, replaced only by an admin or the report's owner. Its
-- token is also kept Fernet-encrypted (token_encrypted) so the box can show it
-- again; other keys keep only the hash. One per report.

ALTER TABLE api_key ADD COLUMN token_encrypted TEXT;
ALTER TABLE api_key ADD COLUMN excel_link INTEGER NOT NULL DEFAULT 0
    CHECK (excel_link = 0 OR (scope_type = 'report' AND token_encrypted IS NOT NULL));

CREATE UNIQUE INDEX idx_api_key_excel_link ON api_key(report_id) WHERE excel_link = 1;
