-- How a report's rows are shown: formats, alignment, hidden columns, links, styles,
-- totals and subtotals (services/presentation.py). Presentation never changes the data.
--
-- report.presentation: one JSON document (Presentation.to_json), NULL = no settings:
-- it is only ever read and written whole with its report, nothing queries into it,
-- and a definition file's [[report.column]] / [[report.style]] / subtotals_by /
-- total_row keys map onto it one to one.
-- report.presentation_problem: what in the settings no longer fits the report's live
-- columns (a column gone after a rebuild or a body edit); NULL when everything fits.
-- Rendering skips the broken parts; owners see the problem.
ALTER TABLE report ADD COLUMN presentation TEXT;
ALTER TABLE report ADD COLUMN presentation_problem TEXT;
