# Strata Roadmap

## Completed

### Phase 1: Project Skeleton & Core Infrastructure

- [x] Project directory structure
- [x] `pyproject.toml` with dependencies (flask, apsw, duckdb, gatekeeper, outbox, openpyxl, click, gunicorn, python-dotenv)
- [x] `Makefile` (sync, rundev, run, check, init-db, worker, config-list, config-set)
- [x] `database/schema.sql` -- full schema (report, report_parameter, tag, report_tag, report_access, report_run, schedule, api_link, report_fts, app_setting, db_metadata)
- [x] `db.py` -- APSW connection management with Flask-context and standalone transactions
- [x] `config.py` -- typed config registry (server, cache, worker, proxy settings)
- [x] `__init__.py` -- app factory with Gatekeeper init, Jinja filters (localdate, localdatetime, duration), timezone handling
- [x] `web.py` and `wsgi.py` -- web entry point with Click + gunicorn
- [x] `cli.py` -- admin CLI (init-db, config list/get/set/export, cleanup)
- [x] `blueprints/auth.py` -- Gatekeeper SSO (login_required, admin_required, login, verify, logout)
- [x] Static assets: PicoCSS, HTMX, tom-select, app.css (compact overrides), app.js (theme toggle + timezone)
- [x] `templates/base.html` -- nav, flash messages, dark/light toggle
- [x] `Dockerfile` + `docker-compose.yml`
- [x] `CLAUDE.md`

### Phase 2: Report CRUD & Query Execution

- [x] `models/report.py` -- Report dataclass with get_by_id, get_by_uuid, get_all, create, update, delete
- [x] `models/parameter.py` -- Parameter dataclass with CRUD and sync_parameters for auto-extraction
- [x] `services/template_service.py` -- Jinja AST parsing, `$var` regex scanning, parameter extraction, structural rendering, identifier sanitisation, type casting
- [x] `services/query_service.py` -- DuckDB execution with QueryResult dataclass, compute_result_hash
- [x] `blueprints/reports.py` -- full CRUD + run with parameter forms, HTMX sort/filter
- [x] `templates/reports/` -- edit.html, run.html, _results.html, _parameters.html
- [x] Results table with HTMX sort by column headers and text filter

### Phase 3: Tags, Search, Access Control

- [x] `models/tag.py` -- 32-colour palette tag model
- [x] `models/report_tag.py` -- junction table operations
- [x] `blueprints/tags.py` -- tag CRUD + tom-select JSON search endpoint
- [x] `services/search_service.py` -- FTS5 indexing and search
- [x] `models/report_access.py` -- ACL model (user/group x run/edit)
- [x] `services/access_service.py` -- can_run, can_edit, accessible_report_ids (open by default, creator always has edit)
- [x] `templates/tags/` -- index.html, edit.html with colour picker
- [x] `templates/reports/_tags.html` -- tag display partial

### Phase 4: Caching, Export, Run History

- [x] `services/cache_service.py` -- DuckDB file write/read with sort/filter, hash-addressed storage, purge_old_cache
- [x] `services/export_service.py` -- XLSX and Parquet generation, format dispatcher, openpyxl Excel table formatting, auto-fit columns
- [x] `models/report_run.py` -- run history tracking (create_running, mark_completed, mark_failed, purge_old)
- [x] Run history endpoints: list runs per report, view past run from cache, download (XLSX/Parquet with `?format=` parameter)
- [x] HTMX sort/filter on cached results
- [x] `cli.py` cleanup command for old runs and orphaned cache files
- [x] `templates/runs/` -- index.html, view.html

### Phase 5: Scheduling, Email, API Links, Client Library

- [x] `models/schedule.py` -- schedule model with JSON definition, get_due, create with next_run calculation, update_after_run
- [x] `services/schedule_service.py` -- pure Python next-run calculation (interval, daily, weekly, monthly_day, monthly_pattern, one_time) with working day logic
- [x] `blueprints/schedules.py` -- schedule CRUD with conditional form fields, preview next 5 runs via HTMX
- [x] `worker/schedule_worker.py` -- polls for due schedules, runs reports, caches results, generates XLSX, sends email via Outbox, calculates next run, graceful shutdown
- [x] `services/email_service.py` -- HTML table inline (if <= max_inline_rows) + XLSX attachment via Outbox client
- [x] `models/api_link.py` -- GUID-based links with expiry, rotation, use tracking
- [x] `blueprints/api.py` -- PowerQuery XLSX/JSON endpoints, API link management (create, rotate, delete), programmatic report execution, run retrieval/download
- [x] `client/client.py` -- StrataClient facade (local + HTTP modes)
- [x] `client/models.py` -- ReportResult, ReportSummary, ParameterInfo dataclasses
- [x] `client/backends/base.py` -- StrataBackend protocol
- [x] `client/backends/local.py` -- direct SQLite + DuckDB backend
- [x] `client/backends/http.py` -- remote HTTP API backend (httpx)
- [x] `blueprints/admin.py` -- admin dashboard with stats, configuration editor, read-only SQL console
- [x] `templates/schedules/` -- index.html, edit.html (conditional fields per type, weekday checkboxes, preview), _preview.html
- [x] `templates/admin/` -- index.html, config.html, sql.html
- [x] Registered all blueprints (auth, reports, tags, schedules, api, admin) in app factory
- [x] Nav links for Tags and Admin in base template

---

## Future Work

### SQL Editor & Autocomplete

- [ ] Replace the plain `<textarea>` with CodeMirror (lighter than Monaco, better fit for server-rendered pages)
- [ ] DuckDB SQL keyword highlighting and syntax validation
- [ ] Parameter name completion -- report's `$var` and `{{ var }}` names are already known from `Parameter.get_for_report()`, pass them to the editor as a custom completion source
- [ ] Server-side table/column completion via DuckDB's `sql_auto_complete()` function -- requires data sources to be configured (see below) so there is something to introspect
- [ ] Consider DuckDB-WASM for client-side SQL parsing and validation without a server round-trip
- [ ] Schema introspection endpoint (e.g. `GET /api/v1/datasources/<name>/schema`) that connects to a named data source and returns `information_schema.tables` and `information_schema.columns` as JSON, cached for editor use

### Data Sources & Persistent Connections

Currently every query runs against `duckdb.connect(":memory:")` with no pre-existing tables. This section adds named, reusable data sources.

- [ ] `data_source` table in SQLite -- `(id, name, type, config_json, created_by, created_at)` where type covers DuckDB extension connection methods (nanodbc/ODBC, mssql, httpfs, sqlite_scanner, postgres_scanner, etc.) and config holds the connection string or extension-specific parameters
- [ ] Data source management UI -- create, edit, test connection, delete
- [ ] Query engine changes: before executing a report, `ATTACH` all referenced data sources by name so that report SQL can use `datasource_name.schema.table` syntax
- [ ] Data sources referenced via structural parameters (`{{ connection }}`) remain supported for backwards compatibility; named data sources provide a managed alternative that doesn't require the user to know connection strings
- [ ] Connection testing endpoint to verify a data source is reachable before saving

### Datasets

Datasets are reports flagged as reusable data sources that other reports can reference by name in their SQL.

- [ ] `is_dataset` flag on reports -- marks a report as a dataset
- [ ] Reference syntax: other reports use `dataset.<name>` in SQL, which resolves to the dataset's cached `.duckdb` file via `ATTACH '{cache_path}' AS dataset (READ_ONLY)` before query execution
- [ ] Live mode: if a dataset's cache TTL has expired (or TTL is 0), re-execute the dataset before running the referencing report; otherwise use the cached result
- [ ] Configurable cache TTL per dataset (default: use scheduled refresh; 0 = always re-execute)
- [ ] Remote datasets: a dataset can reference another Strata server, fetched via the HTTP client backend with an API key, cached locally as a `.duckdb` file
- [ ] Dependency awareness: detect dataset references in SQL and ensure datasets are fresh before the referencing report runs (no complex DAG resolution initially -- just direct dependencies)

### Query Engine Enhancements

- [ ] EXPLAIN preview on the report edit page -- run `EXPLAIN {rendered_sql}` and show the DuckDB operator tree as preformatted text; helps authors spot full scans and understand query plans before executing (note: LIMIT wrapping is unreliable for multi-statement SQL scripts, so EXPLAIN is the better approach)
- [ ] Query timeout configuration (per-report or global)
- [ ] Result row limit configuration (per-report or global)

### Comments & Annotations

- [ ] Comments on report definitions -- design notes, usage guidance, change history notes
- [ ] Comments on report runs -- context for a specific execution ("month-end snapshot", "data quality issue in rows X--Y")
- [ ] Index comments in FTS5 so they are searchable alongside report name, description, and tags
- [ ] Run retention protection -- tag or flag individual runs to prevent deletion during the 30-day cleanup cycle (e.g. "keep" flag or an explicit expiry date override on the run)

### Shareable Result URLs

- [ ] Encode sort/filter state in URL query params (`?sort_col=X&sort_dir=desc&filter_text=Y`)
- [ ] HTMX pushes current sort/filter state to the browser URL (via `hx-push-url` or `history.replaceState`) so the URL is always copyable with the current view
- [ ] When a shared URL is opened, load the cached results and reapply sort/filter -- no new filesystem storage needed
- [ ] Consider re-running the base query when a different user opens a shared URL to avoid showing stale data

### Homepage & Activity

- [ ] Per-user recent activity feed -- report creations, edits, and runs by the current user
- [ ] Quick-access list of recently run reports
- [ ] Favourite reports (star/unstar) with a favourites section on the home page

### Tag Enhancements

- [ ] Tag overview page showing report count per tag
- [ ] Ensure tags are surfaced as search terms in the report listing UI (FTS5 already indexes tags_text)

### Access Control UI

- [ ] Access control management on the report edit page (add/remove user/group permissions)
- [ ] Report listing filtered by access permissions
- [ ] Search results filtered by access

### Report List Enhancements

- [ ] `reports/_list.html` HTMX partial for filtered/searched report listing
- [ ] Tag filtering on report list page
- [ ] FTS5 search integration in report listing

### Schedule Enhancements

- [ ] Holiday table for bank holiday awareness in working day calculations
- [ ] Schedule run history log (success/failure per execution)
- [ ] Email notification on schedule failure
- [ ] Multiple daily times support in schedule UI
- [ ] Exception report mode on schedules -- schedule runs on its usual cadence but only sends
  the email if the report query returned more than 0 rows or if execution failed; useful for
  alert/monitor-style reports that function like a test runner (all-clear runs are silent)

### API Enhancements

- [ ] API key authentication for programmatic endpoints (currently uses session auth)
- [ ] `GET /api/v1/reports` -- list reports endpoint for HTTP client backend
- [ ] Rate limiting on API link endpoints
- [ ] API link management UI in the web interface (currently JSON API only)

### CSV Export

- [ ] Add CSV as a download format alongside XLSX and Parquet
- [ ] Implement via DuckDB `COPY results TO ... (FORMAT CSV, HEADER)` in export_service
- [ ] Add `csv` entry to `_FORMAT_MAP` with `text/csv` mimetype and `.csv` extension

### UI Polish

- [ ] Multi-sort via shift-click on column headers
- [ ] Pagination for large result sets
- [ ] Report cloning / duplicate
- [ ] Report version history / audit log

### Export & Delivery

- [ ] Scheduled delivery to SharePoint / file share

### Operations

- [ ] Health check endpoint
- [ ] Prometheus metrics
- [ ] Structured JSON logging
- [ ] Database backup CLI command
- [ ] Migration system for schema changes
