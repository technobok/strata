# Claude Code Instructions

## Build/Run Commands
```bash
make sync                # Install dependencies (local dev)
make init-db             # Create / migrate the local dev DB
make rundev              # Run dev server
make check               # lint + test — run before every commit
make lint                # ruff format + ruff check + ty check
make test                # pytest suite (tests/; fresh DB + DATA_ROOT per test)
make config-list         # Show config
make config-set KEY=key VAL=value  # Set config
strata-admin reconcile --dry-run   # Retention + orphaned-file sweep (drop --dry-run to apply)

# Docker (deployment)
docker compose build && docker compose up -d   # both containers migrate on start (race-safe)
make rebuild             # Rebuild images + apply DB migrations
make db-init             # Apply DB migrations only (no rebuild, no restart)
```

## Architecture
- Backend: Python Flask + APSW (SQLite metadata) + DuckDB (build + query engine). `pytz` is required (DuckDB needs it to fetch TIMESTAMPTZ results).
- Frontend: HTMX + PicoCSS (compact styling) + dark/light mode + vendored CodeMirror (attaches to `textarea.sql-editor`).
- Core object: a **datamodule** — a versioned DuckDB database built by a script, stored under `DATA_ROOT/<uuid[:2]>/<uuid>/{versions,sources,tmp}/` (dev default `instance/data`). "Live" is a SQLite pointer (`datamodule.live_version_id`) to an immutable snapshot; rebuilds atomic-swap a new file (TTL pull, single-flight via a cross-process lease on the datamodule row: `BuildLease` in `datamodule_service`). Retention prunes old versions (rows AND files) after each build; `strata-admin reconcile` sweeps the rest.
- Build inputs: uploaded source files (`source('name')` in scripts), dependencies (other datamodules ATTACHed by local name, TTL-pulled, cycle-rejected), and `connection`-table databases via `connection_service.attach_into`.
- **The serve path never builds**: `reports.serve` resolves the version via `build_queue.servable_version` (fresh → serve; stale → coalesced unforced `stale-read` job + bounded wait `serve.max_wait_seconds` → else serve the last good version marked stale; failure backoff `consecutive_failures`/`retry_after`). `report_service.run_table/stream` take the version to serve and never build.
- Reports are DuckDB **macros** layered on a datamodule, served over the live version at `/r/<uuid>.<ext>` (html/parquet/csv/json/xlsx). Bodies use `$arg` for macro parameters (internally prefixed `arg_`).
- Serve query grammar (`services/query_filter.py`): declared args by name; `col__op=value` filters (eq/ne/gt/gte/lt/lte/in/between/contains/isnull); bare `col=v`; `q=` all-column search; `sort=-a,b`; `page=N` (200/page); `version=<version_uuid>` pins a historical snapshot (never rebuilds). Fields are DESCRIBE-validated, values parameter-bound; unknown keys error. Reserved (rejected as arg names): sort, page, q, format, key, version.
- API keys: sha256-hashed bearer tokens (`?key=`) grant unauthenticated access to **data formats only** (html stays SSO); datamodule- or report-scoped; fixed args always win, optional whitelist restricts caller args; every keyed request is access-logged. Plaintext token shown exactly once at create/rotate.
- Schema is migration-managed: `database/migrations/NNNN_*.sql` applied forward-only by `db.apply_migrations` (ledger `schema_migrations`, `EXPECTED_SCHEMA_VERSION`).
- Auth: Gatekeeper client (SSO). Email via Outbox arrives with the Phase B/C scheduler + exception reports (see ROADMAP.md); `mail.*`/`worker.*` config entries are reserved for it.

## Key Patterns
- Tests drive the real routes through the Flask test client (`tests/conftest.py`): a fresh SQLite DB + DATA_ROOT per test, an injected admin user (`X-User: viewer` = non-admin, `X-Anon: 1` = anonymous), and the `strata` fixture's authoring helpers.
- App factory in `src/strata/__init__.py`
- APSW db module with Flask-context and standalone transactions in `src/strata/db.py`
- Typed config registry in `src/strata/config.py`. Each entry says when a change applies (`Applies`). `Applies.LIVE` settings (build.*, serve.*, schedule.*, worker.poll_interval) are read at use with `config.live_str/int/bool/list` (app_setting, 5 s cache, dropped on every write in-process) and are NOT in `KEY_MAP`/app.config; the rest are loaded into app.config at `create_app`. Tests change settings with `strata.set_config({...})` (posts /admin/config), never by poking app.config.
- All models are dataclasses with static CRUD methods
- Blueprints in `src/strata/blueprints/`
- Services in `src/strata/services/`
- External DB connections live in the `connection` table, params encrypted with Fernet keyed off `app_setting.secret_key`. Adding a driver = registering a `DriverSpec` in `src/strata/services/connection_service.py:DRIVERS`.
- **Every DuckDB connection that runs author SQL goes through `services/duck.py`** (`serve_conn`, `lock_down_build`): trusted setup (ATTACH, extension LOAD) first, then `lock_down` (external access off, allowed directories, locked config). Never call `duckdb.connect` for author SQL directly. Report bodies/constraint queries must pass `duck.validate_select` (exactly one SELECT). `build.sandbox` (config) is the build-side escape hatch.
- **Builds run in `strata-worker`** (`src/strata/worker.py`): `build_queue.request_build` records a coalesced `build_job`; workers claim with one `UPDATE … RETURNING`. Tests and `strata-web --dev` set `WORKER_INLINE` so requested builds run synchronously in the caller (`tests/test_worker.py` turns it off to exercise the queue).
- **Schedules are RFC 5545 RRULEs** (`services/schedule_service.py`, python-dateutil): the builder form → `Definition` JSON (`kind`, `fields`, `timezone`, `rrule`, `dtstart`); the `rrule` kind is the escape hatch for new shapes (no code). `schedule_service.run_due` (the worker's tick) queues forced builds; `Schedule.claim_run` is a compare-and-set on `next_run_at`.
- Flash messages auto-dismiss after 5s (`app.js`) — persistent notices (version banner, token reveal) use their own classes, never `.flash`.

## Git Commits
- Do not add "Co-Authored-By" lines to commit messages
