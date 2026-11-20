# Claude Code Instructions

## Build/Run Commands
```bash
make sync                # Install dependencies (local dev)
make init-db             # Create / migrate the local dev DB
make rundev              # Run dev server
make check               # ruff format + ruff check + ty check
make config-list         # Show config
make config-set KEY=key VAL=value  # Set config
strata-admin reconcile --dry-run   # Retention + orphaned-file sweep (drop --dry-run to apply)

# Docker (deployment)
make rebuild             # After git pull: rebuild images + apply DB migrations
make db-init             # Apply DB migrations only (no rebuild)
```

## Architecture
- Backend: Python Flask + APSW (SQLite metadata) + DuckDB (build + query engine). `pytz` is required (DuckDB needs it to fetch TIMESTAMPTZ results).
- Frontend: HTMX + PicoCSS (compact styling) + dark/light mode + vendored CodeMirror (attaches to `textarea.sql-editor`).
- Core object: a **datamodule** — a versioned DuckDB database built by a script, stored under `DATA_ROOT/<uuid[:2]>/<uuid>/{versions,sources,tmp}/` (dev default `instance/data`). "Live" is a SQLite pointer (`datamodule.live_version_id`) to an immutable snapshot; rebuilds atomic-swap a new file (TTL pull, in-process single-flight). Retention prunes old versions (rows AND files) after each build; `strata-admin reconcile` sweeps the rest.
- Build inputs: uploaded source files (`source('name')` in scripts), dependencies (other datamodules ATTACHed by local name, TTL-pulled, cycle-rejected), and `connection`-table databases via `connection_service.attach_into`.
- Reports are DuckDB **macros** layered on a datamodule, served over the live version at `/r/<uuid>.<ext>` (html/parquet/csv/json/xlsx). Bodies use `$arg` for macro parameters (internally prefixed `arg_`).
- Serve query grammar (`services/query_filter.py`): declared args by name; `col__op=value` filters (eq/ne/gt/gte/lt/lte/in/between/contains/isnull); bare `col=v`; `q=` all-column search; `sort=-a,b`; `page=N` (200/page); `version=<version_uuid>` pins a historical snapshot (never rebuilds). Fields are DESCRIBE-validated, values parameter-bound; unknown keys error. Reserved (rejected as arg names): sort, page, q, format, key, version.
- API keys: sha256-hashed bearer tokens (`?key=`) grant unauthenticated access to **data formats only** (html stays SSO); datamodule- or report-scoped; fixed args always win, optional whitelist restricts caller args; every keyed request is access-logged. Plaintext token shown exactly once at create/rotate.
- Schema is migration-managed: `database/migrations/NNNN_*.sql` applied forward-only by `db.apply_migrations` (ledger `schema_migrations`, `EXPECTED_SCHEMA_VERSION`).
- Auth: Gatekeeper client (SSO). Email via Outbox arrives with the Phase B/C scheduler + exception reports (see ROADMAP.md); `mail.*`/`worker.*` config entries are reserved for it.

## Key Patterns
- App factory in `src/strata/__init__.py`
- APSW db module with Flask-context and standalone transactions in `src/strata/db.py`
- Typed config registry in `src/strata/config.py`
- All models are dataclasses with static CRUD methods
- Blueprints in `src/strata/blueprints/`
- Services in `src/strata/services/`
- External DB connections live in the `connection` table, params encrypted with Fernet keyed off `app_setting.secret_key`. Adding a driver = registering a `DriverSpec` in `src/strata/services/connection_service.py:DRIVERS`.
- Flash messages auto-dismiss after 5s (`app.js`) — persistent notices (version banner, token reveal) use their own classes, never `.flash`.

## Git Commits
- Do not add "Co-Authored-By" lines to commit messages
