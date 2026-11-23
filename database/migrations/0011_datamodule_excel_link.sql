-- One Excel link per DATAMODULE (user decision 2026-11-23), replacing one per report.
--
-- Access is set per datamodule, so its Excel link is too: one key, shown in the
-- "Use in Excel" box of every report on the datamodule; replacing it cuts off every
-- workbook on the datamodule at once.
--
-- datamodule_excel_link = 1 marks the datamodule's Excel link (scope 'datamodule',
-- token kept encrypted so the box can show it again; one per datamodule). The 0008
-- column excel_link now marks a report's Excel link from before this change: they
-- keep working, are no longer shown, and are disabled when an owner replaces the
-- datamodule's link. (A column is added rather than 0008's CHECK changed: that needs
-- a table rebuild, and dropping api_key would cascade into api_key_access_log.)
ALTER TABLE api_key ADD COLUMN datamodule_excel_link INTEGER NOT NULL DEFAULT 0
    CHECK (datamodule_excel_link = 0 OR (scope_type = 'datamodule'
           AND datamodule_id IS NOT NULL AND token_encrypted IS NOT NULL));

CREATE UNIQUE INDEX idx_api_key_datamodule_excel_link
    ON api_key(datamodule_id) WHERE datamodule_excel_link = 1;

UPDATE api_key SET name = 'Excel link (one report, before 2026-11-23)' WHERE excel_link = 1;
