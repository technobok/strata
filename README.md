# Strata

A reporting system that uses DuckDB as its query engine, allowing users to author, run, schedule, and distribute parameterised SQL reports across multiple data sources.

## Features

- **Two-layer SQL templating** — `{{ var }}` for Jinja2 structural parts (table names, connection strings), `$var` for DuckDB bind parameters (dates, codes, filter values)
- **External-DB connections** — encrypted connection records (SQLite, PostgreSQL, ODBC) declared inline in report SQL via `{% do conn('name') %}`
- **Materialised reports** — opt a report into writing its result to a named DuckDB file; reference it from other reports via `{{ ref('name') }}`
- **Automatic parameter extraction** — Jinja AST parsing and regex scanning detect parameters from SQL templates
- **DuckDB result cache** — query results cached as DuckDB files, enabling fast re-sorting, filtering, and export without re-execution
- **HTMX results table** — sort and filter cached results inline without page reload
- **Multi-format export** — download results as XLSX (formatted Excel tables via openpyxl) or Parquet, with a `?format=` parameter on all download endpoints
- **Custom scheduling** — user-friendly JSON schedule definitions with business-day awareness (first/last working day of month), no cron syntax
- **Email delivery** — scheduled reports sent via Outbox with inline HTML tables and XLSX attachments
- **PowerQuery API links** — GUID-based URLs for Excel/Power BI to pull report data directly as XLSX, Parquet, or JSON
- **Programmatic API** — JSON endpoints for running reports and retrieving results
- **Client library** — `StrataClient` with local (direct DB) and HTTP (remote API) backends
- **Tag system** — 32-colour palette for organising reports
- **FTS5 search** — full-text search across report names, descriptions, and tags
- **Access control** — user/group ACLs with run/edit permissions (open by default)
- **Dark/light mode** — PicoCSS with theme toggle
- **Admin panel** — system stats, configuration editor, read-only SQL console with schema sidebar, and external-connection manager

## Stack

- Python 3.14, Flask, APSW (SQLite for metadata), DuckDB (query engine)
- HTMX + PicoCSS + dark/light mode
- [Gatekeeper](../gatekeeper/) client for SSO authentication
- [Outbox](../outbox/) client for email delivery
- [webreports-caddy](../webreports-caddy/) (optional) reverse proxy when serving on a subpath
- openpyxl for XLSX export, cryptography (Fernet) for connection params at rest
- uv for dependency management, ruff + ty for code quality

## Architecture

```
┌────────── platform-net (docker network) ──────────┐
│                                                   │
│   ┌────────────┐   ┌──────────────┐               │
│   │ strata-app │──▶│  strata-     │               │
│   │ (gunicorn) │   │  worker      │               │
│   └─────┬──────┘   └──────┬───────┘               │
│         │                 │                       │
│         │   /app/instance/strata.sqlite3          │
│         │   /cache-data/<hash>.duckdb             │
│         │   /cache-data/named/<name>.duckdb       │
│         ▼                 ▼                       │
│   ┌──────────────────────────────┐                │
│   │ /gatekeeper-data (read user) │  ◀── gatekeeper sibling
│   │ /outbox-data    (queue mail) │  ◀── outbox sibling
│   └──────────────────────────────┘                │
└───────────────────────────────────────────────────┘
            ▲
            │  reverse proxy (optional, e.g. webreports-caddy)
            ▼
        clients (browser, Power BI, scripts)
```

Strata runs two long-lived containers: **`strata-app`** (gunicorn, serves the web UI + APIs) and **`strata-worker`** (background loop that runs scheduled reports). Both share an SQLite metadata DB (`strata.sqlite3`) and a result cache directory of DuckDB files. Authentication and email delivery are delegated to sibling services — `gatekeeper` (for SSO) and `outbox` (for SMTP queueing) — whose own SQLite files are mounted in read-write so strata's clients can talk to them locally.

## Local development

```bash
# Install dependencies
make sync

# Create / migrate the local dev DB at instance/strata.sqlite3
make init-db

# Run the dev server (127.0.0.1:5000, debug mode)
make rundev

# In another terminal, run the schedule worker
make worker
```

Strata can run standalone in dev — gatekeeper is optional. Without gatekeeper configured, authentication is disabled and there's no admin path; that's fine for local hacking on report features. To wire up local SSO + email:

```bash
export GATEKEEPER_DB=/abs/path/to/gatekeeper/instance/gatekeeper.sqlite3
export OUTBOX_DB=/abs/path/to/outbox/instance/outbox.sqlite3
make rundev
```

The dev server picks up env vars on launch; restart after changing them.

## Production deployment with Docker

This is the full path from "fresh git clone on a server" to "logged-in admin viewing reports". Each numbered step depends on the ones above it.

### 1. Prerequisites

- Docker Engine and Compose v2 (`docker compose version` ≥ 2).
- A `platform-net` Docker network (shared by strata, gatekeeper, outbox):
  ```bash
  docker network create platform-net
  ```
- Sibling checkouts of [`gatekeeper`](../gatekeeper/) and [`outbox`](../outbox/). By default strata expects them at `../gatekeeper` and `../outbox` relative to its own directory; the paths are overridable via `GATEKEEPER_INSTANCE` and `OUTBOX_INSTANCE`.
- (Optional) [`webreports-caddy`](../webreports-caddy/) if you want to serve strata behind a reverse proxy at a subpath like `/strata`.

### 2. Bring up the sibling services first

Strata can start without them but won't be useful (no auth, no email). Follow each project's README:

```bash
cd ../gatekeeper && make rebuild && docker compose up -d
cd ../outbox     && make rebuild && docker compose up -d
cd ../strata
```

### 3. Pin host paths with a `docker-compose.override.yml`

The base `docker-compose.yml` defaults to local paths under `./instance/`. For a real deployment you typically want the SQLite metadata DB and the (potentially large) result cache on persistent storage. `docker-compose.override.yml` is gitignored and is the right place for site-specific values:

```yaml
# docker-compose.override.yml — production paths on xauuxweb01 as an example
services:
  app:
    volumes:
      - /mnt/data/webreports_data/strata:/app/instance
      - /mnt/data/strata_data:/cache-data
      - /mnt/data/webreports_data/gatekeeper:/gatekeeper-data
      - /mnt/data/webreports_data/outbox:/outbox-data
    ports:
      - "5005:5005"
    environment:
      - PORT=5005
      - HOST=0.0.0.0

  worker:
    volumes:
      - /mnt/data/webreports_data/strata:/app/instance
      - /mnt/data/strata_data:/cache-data
      - /mnt/data/webreports_data/gatekeeper:/gatekeeper-data
      - /mnt/data/webreports_data/outbox:/outbox-data
```

### 4. Build images and apply schema migrations

```bash
docker compose build
make db-init        # creates instance/strata.sqlite3 if absent, applies migrations
docker compose up -d
```

`make db-init` runs `strata-admin init-db` inside a throwaway container based on the *just-built* `app` image, so the binary applying migrations is always in sync with what the long-lived containers will run. There is no separate "init image" to keep current.

If you start `app`/`worker` against a DB whose `schema_version` is older than the code expects (e.g. you forgot `make db-init` after a `git pull`), the app aborts at boot with a clear message pointing back at `make db-init`. You won't accidentally serve traffic on a broken schema.

### 5. Bootstrap your first admin

Strata's admin gate reads the gatekeeper user's group memberships. The default admin group is `strata-admins` (overridable via the `STRATA_ADMIN_GROUP` env var). Until at least one user is in that group, nobody can reach `/admin/*`.

In gatekeeper's web UI:

1. Log in as a gatekeeper admin.
2. Groups → **New** → name it `strata-admins` → save.
3. Groups → `strata-admins` → **Members** → add the gatekeeper user accounts who should manage strata.

Then log in to strata. The **Admin** link will be in the top nav. From `/admin` you can reach:

- **SQL Console** — read-only SELECT/PRAGMA/EXPLAIN against the metadata DB, with a schema sidebar.
- **Configuration** — DB-stored settings registry (`server.port`, `cache.directory`, etc.).
- **Connections** — register external databases so reports can `ATTACH` them. See *Connecting via ODBC* below for the MSSQL setup.

### 6. After every `git pull`

```bash
git pull
make rebuild        # docker compose build + make db-init
docker compose up -d
```

`make rebuild` is the "one button" recipe — it rebuilds images and applies any new schema migrations in the right order.

### 7. Reverse-proxy notes

Strata's compose file ships with `PROXY_X_FORWARDED_PREFIX=1` (and the other three `PROXY_X_FORWARDED_*` flags) so `url_for()` produces correct URLs when fronted at a subpath. If you front strata with Caddy at `/strata`, make sure the proxy strips the prefix and sends `X-Forwarded-Prefix: /strata`. The reference setup is [`webreports-caddy`](../webreports-caddy/), whose Caddyfile contains a working `handle_path /strata/* { reverse_proxy strata-app:5005 { header_up X-Forwarded-Prefix /strata } }` block.

Standalone deployments (no proxy) can leave the env vars at `1` — ProxyFix on a non-proxied request is a no-op because the headers simply aren't present.

### 8. Connecting via ODBC

The runtime image bundles unixODBC and the **FreeTDS** ODBC driver (pure-MIT open source), so an `odbc` Connection works out of the box without any host-side setup. FreeTDS supports the `UID=DOMAIN\user;PWD=…;UseNTLMv2=yes;Trusted_Domain=DOMAIN` auth pattern used by Windows-domain-joined SQL Server, which Microsoft's Linux ODBC driver (`msodbcsql18`) deliberately does not — that's why we ship FreeTDS only.

In strata, **/admin/connections → New → "ODBC (any driver…)"** opens a single text box for the connection string.

Worked example for a domain-joined SQL Server (NTLM):

```
Driver=FreeTDS;Server=host.example.com;Port=1433;Database=mydb;UID=DOMAIN\user;PWD=...;UseNTLMv2=yes;Trusted_Domain=DOMAIN;TDS_Version=7.4
```

For SQL-native logins (no domain prefix), drop the NTLM fields:

```
Driver=FreeTDS;Server=host;Port=1433;Database=mydb;UID=sqluser;PWD=...;TDS_Version=7.4
```

The string is Fernet-encrypted before it lands in the SQLite metadata DB; on Test, strata installs `community/odbc_scanner`, runs `LOAD odbc_scanner`, and `ATTACH`es your source as a probe alias (`READ_ONLY`). Reports use the connection by declaring it inline in the SQL — see *SQL templates and references* below.

For PostgreSQL there's no need to use ODBC — the dedicated `postgres` driver uses DuckDB's native postgres extension and is faster.

If you need Always Encrypted, MARS, or AD-password auth with UPN UIDs (`user@domain.com`), add Microsoft's `msodbcsql18` back to the runtime stage of the `Dockerfile` (see the comment block in there). Bring your own apt-repo workaround — Microsoft's signing key was still SHA1-self-signed at the time we last looked, which Debian 12's apt now rejects.

## Configuration reference

Strata reads settings from three layers, in priority order: **env vars > DB-stored config (`app_setting` table) > built-in defaults**. The DB-stored layer is editable from `/admin/config` or via `make config-set KEY=key VAL=value`.

### Environment variables

| Variable | Default | Purpose | Where set |
|---|---|---|---|
| `HOST` | `0.0.0.0` | Bind address for gunicorn / Flask. | Compose `app` |
| `PORT` | `5000` | Listen port; also drives the host port mapping. | Compose `app` |
| `STRATA_DB` | `/app/instance/strata.sqlite3` | Path to the SQLite metadata DB. | Compose (commented) |
| `STRATA_ROOT` | (auto) | Project root used to resolve `instance/` and `database/schema.sql` when running outside a source checkout. | Compose (commented) |
| `STRATA_CACHE_DIR` | `./instance/cache` (host) | Host directory mounted at `/cache-data`. Pin to a dedicated disk for large caches. | Compose (host substitution) |
| `CACHE_DIRECTORY` | `/cache-data` (container) | Where DuckDB cache and named-materialised files land inside the container. | Compose `app` + `worker` |
| `GATEKEEPER_INSTANCE` | `../gatekeeper/instance` (host) | Host path to the sibling gatekeeper's `instance/` dir, mounted at `/gatekeeper-data`. | Compose (host substitution) |
| `GATEKEEPER_DB` | `/gatekeeper-data/gatekeeper.sqlite3` | **Required for SSO.** Path to gatekeeper's SQLite. | Compose `app` + `worker` |
| `STRATA_ADMIN_GROUP` | `strata-admins` | **Required to access `/admin/*`.** Gatekeeper group whose members are strata admins. | Compose `app` + `worker` |
| `OUTBOX_INSTANCE` | `../outbox/instance` (host) | Host path to outbox's `instance/` dir, mounted at `/outbox-data`. | Compose (host substitution) |
| `OUTBOX_DB` | `/outbox-data/outbox.sqlite3` | Path to outbox's SQLite. Required for scheduled email delivery. | Compose `app` + `worker` |
| `MAIL_SENDER` | `strata@localhost` | From-address on outgoing schedule emails. | Compose (commented) |
| `PROXY_X_FORWARDED_FOR` | `1` | Trust hop count for `X-Forwarded-For` (audit log client IP). | Compose `app` + `worker` |
| `PROXY_X_FORWARDED_PROTO` | `1` | Trust hop count for `X-Forwarded-Proto`. | Compose `app` + `worker` |
| `PROXY_X_FORWARDED_HOST` | `1` | Trust hop count for `X-Forwarded-Host`. | Compose `app` + `worker` |
| `PROXY_X_FORWARDED_PREFIX` | `1` | Trust hop count for `X-Forwarded-Prefix`. **Required when behind a subpath proxy** so `url_for()` produces correct URLs. | Compose `app` + `worker` |
| `TZ` | `UTC` | Container timezone. UI renders timestamps in the user's local TZ regardless. | Compose `app` + `worker` |

`CACHE_DIRECTORY` and the four `PROXY_X_FORWARDED_*` values are also exposed as DB-stored keys (`cache.directory`, `proxy.x_forwarded_*`); env wins when both are set.

### DB-stored configuration

The full registry is in `src/strata/config.py`. Notable keys: `server.port`, `server.host`, `cache.directory`, `cache.retention_days`, `proxy.x_forwarded_*`. Edit through the admin UI or:

```bash
make config-list
make config-set KEY=cache.retention_days VAL=60
```

## SQL templates and references

Reports use a Jinja2 template plus DuckDB bind parameters:

```sql
-- {{ var }}              Jinja structural (rendered before execution)
-- $var                   DuckDB bind parameter (typed, executed safely)
-- {% do conn('name') %}  attach an external connection under the given name
-- {{ ref('name') }}      reference another materialised report

SELECT *
FROM {{ schema }}.{{ table_name }}
WHERE company_code = $company_code
  AND date >= $date_from
```

Structural parameters are validated (alphanumeric, underscores, dots, `$`). Value parameters are type-cast to their declared DuckDB type before execution. When saving a report, parameters are automatically extracted from the SQL template and synced with the parameter definitions.

### External connections — `conn('name')`

Define a connection at `/admin/connections`, then declare it inline at the top of the report SQL. The connection's name becomes the SQL alias:

```sql
{% do conn('warehouse') %}
{% do conn('crm') %}

SELECT a.id, b.label, a.amount
FROM   warehouse.dbo.orders a
JOIN   crm.public.customers b ON a.cust_id = b.id
WHERE  a.created_at >= $since
```

Each `conn(...)` call ATTACHes the source under its connection name before the query runs. Connection names are validated as SQL-safe identifiers (start with a letter/underscore, then letters/digits/underscores) so they can be used unquoted as aliases. Multiple connections per report are first-class.

If you want a shorter alias in the SQL than the connection's name (e.g. for a long name), capture it with `{% set %}`:

```sql
{% set wh = conn('nav-prod-warehouse-aus') %}{# ❌ would be rejected: hyphens not allowed #}
{% set wh = conn('nav_prod_warehouse_aus') %}
SELECT * FROM {{ wh }}.dbo.orders
```

### Materialised reports — `ref('name')`

If a report sets **Materialise as** to e.g. `orders_daily`, its result is written to a `result` table inside `cache_dir/named/orders_daily.duckdb` on every run. Other reports can reference it inline:

```sql
SELECT customer, count(*) FROM {{ ref('orders_daily') }} GROUP BY 1
```

`{{ ref('orders_daily') }}` expands to `mat_orders_daily.result` and triggers a read-only `ATTACH` of the matching cache file before execution. Combine with a schedule on the source report to refresh on a cadence.

## Schedule system

Schedules are defined as structured JSON, not cron expressions. The UI presents dropdowns, time pickers, and checkboxes.

Supported schedule types:
- **Interval** — every N minutes/hours/days
- **Daily** — at a specific time (or multiple times)
- **Weekly** — on selected weekdays at a specific time
- **Monthly (day)** — on a specific day of month (1–31, or last)
- **Monthly (pattern)** — first/last working day, first/last day of month
- **One-time** — a specific datetime (auto-disables after execution)

Working days are Monday–Friday. The schedule preview shows the next 5 calculated run times. Missed runs are skipped (no catch-up); the worker calculates the next future occurrence to avoid flooding recipients with duplicate emails.

## API

### PowerQuery / external access

API links provide GUID-based URLs that require no login:

```
GET /api/v1/link/<uuid>?format=xlsx      # Returns XLSX
GET /api/v1/link/<uuid>?format=parquet   # Returns Parquet
GET /api/v1/link/<uuid>/json             # Returns JSON
```

Links support fixed parameters (baked in) and parameterised params (supplied via query string). Links can be rotated (new UUID) or expired.

### Programmatic access

Authenticated JSON API for integration:

```
POST /api/v1/reports/<uuid>/run                    # Run a report, returns JSON results
GET  /api/v1/runs/<run_uuid>                       # Get a run's results
GET  /api/v1/runs/<run_uuid>/download?format=xlsx  # Download (xlsx or parquet)
```

## Client library

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

## Project structure

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
│   │   ├── connections.py       # External-DB connection manager (admin)
│   │   ├── admin.py             # Admin panel, config, SQL console
│   │   ├── api.py               # API: PowerQuery links + programmatic access
│   │   └── tags.py              # Tag management
│   ├── models/                  # Dataclass models with static CRUD methods
│   ├── services/                # Business logic (template, query, cache, connection, etc.)
│   ├── client/                  # Client library (local + HTTP backends)
│   ├── static/                  # CSS, JS, vendor assets
│   └── templates/               # Jinja2 HTML templates
├── worker/
│   └── schedule_worker.py       # Background schedule worker (separate container)
├── database/
│   └── schema.sql               # SQLite schema (idempotent CREATE IF NOT EXISTS)
├── pyproject.toml
├── Makefile
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
└── wsgi.py
```

## Make targets

| Target | Description |
|---|---|
| `make sync` | Install dependencies with uv (local dev) |
| `make init-db` | Create / migrate the local dev DB |
| `make rundev` | Run dev server (debug mode) |
| `make run` | Run production server (gunicorn) |
| `make worker` | Run the schedule worker (foreground) |
| `make check` | Run ruff format + ruff check + ty check |
| `make config-list` | Show all DB-stored configuration |
| `make config-set` | Set a DB-stored config value |
| `make rebuild` | After `git pull`: rebuild images + apply DB migrations |
| `make db-init` | Apply DB migrations against the running deployment |
| `make docker-up` | `docker compose up -d` |
| `make docker-down` | `docker compose down` |
| `make clean` | Remove temp files and the local dev DB |
