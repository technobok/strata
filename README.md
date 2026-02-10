# Strata

A reporting system that uses DuckDB as its query engine, allowing users to author, run, schedule, and distribute parameterised SQL reports across multiple data sources.

## Features

- **Two-layer SQL templating** -- `{{ var }}` for Jinja2 structural parts (table names, connection strings), `$var` for DuckDB bind parameters (dates, codes, filter values)
- **Automatic parameter extraction** -- Jinja AST parsing and regex scanning detect parameters from SQL templates
- **Parquet result cache** -- query results cached as Parquet files, enabling fast re-sorting, filtering, and XLSX export without re-execution
- **HTMX results table** -- sort and filter cached results inline without page reload
- **XLSX export** -- download results as formatted Excel tables (openpyxl)
- **Custom scheduling** -- user-friendly JSON schedule definitions with business-day awareness (first/last working day of month), no cron syntax
- **Email delivery** -- scheduled reports sent via Outbox with inline HTML tables and XLSX attachments
- **PowerQuery API links** -- GUID-based URLs for Excel/Power BI to pull report data directly as XLSX or JSON
- **Programmatic API** -- JSON endpoints for running reports and retrieving results
- **Client library** -- `StrataClient` with local (direct DB) and HTTP (remote API) backends
- **Tag system** -- 32-colour palette for organising reports
- **FTS5 search** -- full-text search across report names, descriptions, and tags
- **Access control** -- user/group ACLs with run/edit permissions (open by default)
- **Dark/light mode** -- PicoCSS with theme toggle
- **Admin panel** -- system stats, configuration editor, read-only SQL console

## Stack

- Python 3.14, Flask, APSW (SQLite for metadata), DuckDB (query engine)
- HTMX + PicoCSS + dark/light mode
- Gatekeeper client for SSO authentication
- Outbox client for email delivery
- openpyxl for XLSX export
- uv for dependency management, ruff + ty for code quality

## Quick Start

```bash
# Install dependencies
make sync

# Create the database
make init-db

# Run the dev server
make rundev

# In another terminal, run the schedule worker
make worker
```

The dev server starts at `http://127.0.0.1:5000` by default.

## Configuration

Set `STRATA_DB` to override the database path (defaults to `instance/strata.sqlite3`).

Set `GATEKEEPER_DB` to enable SSO authentication via Gatekeeper.

Set `OUTBOX_DB` to enable email delivery via Outbox.

```bash
# View all config
make config-list

# Set a config value
make config-set KEY=server.host VAL=0.0.0.0
```

Configuration is stored in the `app_setting` table and loaded at startup.

## Make Targets

| Target | Description |
|--------|-------------|
| `make sync` | Install dependencies with uv |
| `make init-db` | Create a blank database |
| `make rundev` | Run dev server (debug mode) |
| `make run` | Run production server (gunicorn) |
| `make worker` | Run the schedule worker |
| `make check` | Run ruff format + ruff check + ty check |
| `make config-list` | Show all configuration settings |
| `make config-set` | Set a config value |
| `make docker-up` | Start Docker containers |
| `make docker-down` | Stop Docker containers |
| `make clean` | Remove temp files and database |

## SQL Template System

Reports use a two-layer template system:

```sql
-- {{ var }} = Jinja2 structural (rendered first)
-- $var = DuckDB bind parameter (executed safely)

SELECT *
FROM {{ connection_string }}.{{ schema }}.{{ table_name }}
WHERE company_code = $company_code
  AND date >= $date_from
```

Structural parameters are validated (alphanumeric, underscores, dots). Value parameters are type-cast to their declared DuckDB type before execution.

When saving a report, parameters are automatically extracted from the SQL template and synced with the parameter definitions.

## Schedule System

Schedules are defined as structured JSON, not cron expressions. The UI presents dropdowns, time pickers, and checkboxes.

Supported schedule types:
- **Interval** -- every N minutes/hours/days
- **Daily** -- at a specific time (or multiple times)
- **Weekly** -- on selected weekdays at a specific time
- **Monthly (day)** -- on a specific day of month (1--31, or last)
- **Monthly (pattern)** -- first/last working day, first/last day of month
- **One-time** -- a specific datetime (auto-disables after execution)

Working days are Monday--Friday. The schedule preview shows the next 5 calculated run times.

Missed runs are skipped (no catch-up). The worker calculates the next future occurrence to avoid flooding recipients with duplicate emails.

## API

### PowerQuery / External Access

API links provide GUID-based URLs that require no login:

```
GET /api/v1/link/<uuid>         # Returns XLSX
GET /api/v1/link/<uuid>/json    # Returns JSON
```

Links support fixed parameters (baked in) and parameterised params (supplied via query string). Links can be rotated (new UUID) or expired.

### Programmatic Access

Authenticated JSON API for integration:

```
POST /api/v1/reports/<uuid>/run     # Run a report, returns JSON results
GET  /api/v1/runs/<run_uuid>        # Get a run's results
GET  /api/v1/runs/<run_uuid>/download  # Download XLSX
```

## Client Library

```python
from strata.client import StrataClient

# Local mode (direct database access)
client = StrataClient(db_path="/path/to/strata.sqlite3")

# HTTP mode (remote server)
client = StrataClient(server_url="https://strata.example.com", api_key="sk_...")

# Run a report
result = client.run_report("report-uuid", {"date_from": "2026-01-01"})
print(result.columns, result.row_count)

# Get a previous run
result = client.get_run("run-uuid")

# List available reports
reports = client.list_reports()
```

## Project Structure

```
strata/
├── src/strata/
│   ├── __init__.py              # App factory
│   ├── web.py                   # Web entry point
│   ├── cli.py                   # Admin CLI (Click)
│   ├── config.py                # Typed config registry
│   ├── db.py                    # APSW SQLite connection management
│   ├── blueprints/
│   │   ├── auth.py              # Gatekeeper SSO
│   │   ├── reports.py           # Report CRUD + run + results
│   │   ├── schedules.py         # Schedule management
│   │   ├── admin.py             # Admin panel, config, SQL console
│   │   ├── api.py               # API: PowerQuery links + programmatic access
│   │   └── tags.py              # Tag management
│   ├── models/                  # Dataclass models with static CRUD methods
│   ├── services/                # Business logic (template, query, cache, etc.)
│   ├── client/                  # Client library (local + HTTP backends)
│   ├── static/                  # CSS, JS, vendor assets
│   └── templates/               # Jinja2 HTML templates
├── worker/
│   └── schedule_worker.py       # Background schedule worker
├── database/
│   └── schema.sql               # SQLite schema
├── pyproject.toml
├── Makefile
├── Dockerfile
├── docker-compose.yml
└── wsgi.py
```
