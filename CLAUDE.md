# Claude Code Instructions

## Build/Run Commands
```bash
make sync                # Install dependencies
make init-db             # Create blank database
make rundev              # Run dev server
make worker              # Run schedule worker
make check               # ruff format + ruff check + ty check
make config-list         # Show config
make config-set KEY=key VAL=value  # Set config
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

## Git Commits
- Do not add "Co-Authored-By" lines to commit messages
