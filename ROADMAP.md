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

- [x] `services/cache_service.py` -- Parquet write/read with sort/filter, hash-addressed storage, purge_old_cache
- [x] `services/export_service.py` -- XLSX generation with openpyxl Excel table formatting, auto-fit columns
- [x] `models/report_run.py` -- run history tracking (create_running, mark_completed, mark_failed, purge_old)
- [x] Run history endpoints: list runs per report, view past run from cache, XLSX download
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

### Access Control UI

- [ ] Access control management on the report edit page (add/remove user/group permissions)
- [ ] Report listing filtered by access permissions
- [ ] Search results filtered by access

### Report List Enhancements

- [ ] `reports/_list.html` HTMX partial for filtered/searched report listing
- [ ] Tag filtering on report list page
- [ ] FTS5 search integration in report listing
- [ ] Favourite reports on dashboard

### Schedule Enhancements

- [ ] Holiday table for bank holiday awareness in working day calculations
- [ ] Schedule run history log (success/failure per execution)
- [ ] Email notification on schedule failure
- [ ] Multiple daily times support in schedule UI

### API Enhancements

- [ ] API key authentication for programmatic endpoints (currently uses session auth)
- [ ] `GET /api/v1/reports` -- list reports endpoint for HTTP client backend
- [ ] Rate limiting on API link endpoints
- [ ] API link management UI in the web interface (currently JSON API only)
- [ ] CSV download format option alongside XLSX

### Query Engine

- [ ] Persistent DuckDB connections with attached data sources (instead of in-memory per query)
- [ ] Connection string management UI (named data sources)
- [ ] Query timeout configuration
- [ ] Result row limit configuration
- [ ] Query plan / EXPLAIN preview on report edit page

### Export & Delivery

- [ ] PDF export option
- [ ] Scheduled delivery to SharePoint / file share
- [ ] Webhook delivery (POST results to a URL)

### UI Polish

- [ ] SQL editor with syntax highlighting (CodeMirror or similar)
- [ ] Multi-sort via shift-click on column headers
- [ ] Pagination for large result sets
- [ ] Report cloning / duplicate
- [ ] Report version history / audit log
- [ ] Dashboard with recent runs and quick-access reports

### Operations

- [ ] Health check endpoint
- [ ] Prometheus metrics
- [ ] Structured JSON logging
- [ ] Database backup CLI command
- [ ] Migration system for schema changes
