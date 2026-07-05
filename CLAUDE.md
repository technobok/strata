# Claude Code Instructions

## Build/Run Commands
```bash
make sync                # Install dependencies (local dev)
make init-db             # Create / migrate the local dev DB
make rundev              # Run dev server
make worker              # Run schedule worker
make check               # ruff format + ruff check + ty check
make config-list         # Show config
make config-set KEY=key VAL=value  # Set config

# Docker (deployment)
make rebuild             # After git pull: rebuild images + apply DB migrations
make db-init             # Apply DB migrations only (no rebuild)
```

## Architecture
- Backend: Python Flask + APSW (SQLite metadata) + DuckDB (query engine)
- Frontend: HTMX + PicoCSS (compact styling) + dark/light mode
- Core object: a **datamodule** — a versioned DuckDB database built by a script, stored under `instance/data/<uuid[:2]>/<uuid>/versions/`. "Live" is a SQLite pointer (`datamodule.live_version_id`) to an immutable snapshot; rebuilds atomic-swap a new file.
- Reports are DuckDB **macros** layered on a datamodule, served over the live version at `/r/<uuid>.<ext>`. Bodies use `$arg` for macro parameters.
- Schema is migration-managed: `database/migrations/NNNN_*.sql` applied forward-only by `db.apply_migrations` (ledger `schema_migrations`, `EXPECTED_SCHEMA_VERSION`).
- Auth: Gatekeeper client (SSO)
- Email: Outbox client

## Key Patterns
- App factory in `src/strata/__init__.py`
- APSW db module with Flask-context and standalone transactions in `src/strata/db.py`
- Typed config registry in `src/strata/config.py`
- All models are dataclasses with static CRUD methods
- Blueprints in `src/strata/blueprints/`
- Services in `src/strata/services/`
- External DB connections live in the `connection` table, params encrypted with Fernet keyed off `app_setting.secret_key`. Adding a driver = registering a `DriverSpec` in `src/strata/services/connection_service.py:DRIVERS`.

## Git Commits
- Do not add "Co-Authored-By" lines to commit messages
