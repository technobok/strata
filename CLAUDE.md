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
- SQL templates: `{{ var }}` for Jinja structural parts, `$var` for DuckDB bind parameters
- Auth: Gatekeeper client (SSO)
- Email: Outbox client
- Results cached as Parquet files in instance/cache/

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
