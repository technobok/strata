# Strata

A reporting system built around **datamodules**: persistent, versioned DuckDB databases built by SQL scripts, with parameterised reports served as DuckDB macros over the live version — as interactive HTML or streamed Parquet/CSV/JSON/XLSX, including unauthenticated access for Excel/PowerQuery via hashed API keys.

## Features

- **Datamodules** — a DuckDB database built by a plain SQL script; every successful build is an immutable, versioned snapshot; "live" flips atomically, so readers are never disturbed
- **TTL freshness** — request within the TTL serves the current version; past it, the datamodule rebuilds before responding (single-flight; blank TTL = never expire, `0` = always rebuild)
- **Build inputs** — uploaded source files via `source('name')`, other datamodules ATTACHed as dependencies by local name (TTL-pulled, cycles rejected), and external databases (SQLite/PostgreSQL/ODBC) via encrypted connections
- **Reports as macros** — a report is a DuckDB table macro (`$arg` parameters) stored in SQLite and applied at serve time, so editing a report never requires a rebuild
- **Typed arguments** — text/integer/number/decimal/date/time/boolean; input, select, and multiselect widgets; options driven by a constraint query against the live data; defaults and required flags
- **Sort / filter / search grammar** — `field__op=value` filters, full-text `q=` search, `sort=-a,b`, pagination — every field validated against the query's real output columns, every value parameter-bound
- **Multi-format serving** — `/r/<uuid>.html|parquet|csv|json|xlsx`, all streamed with constant memory and exact `Content-Length`
- **Version pinning** — `?version=<version_uuid>` serves any retained historical snapshot (never triggers a rebuild)
- **Retention + reconcile** — per-datamodule keep-newest-N or age-window pruning after each build, plus `strata-admin reconcile` to sweep orphaned files
- **API keys for Excel/PowerQuery** — named, revocable, sha256-hashed bearer tokens (`?key=…`) with per-key fixed/whitelisted arguments and an access log; the full PowerQuery URL is generated for copy/paste when a key is created
- **HTMX results table** — sortable headers, search box, removable filter chips, pager, and export links without page reloads
- **CodeMirror SQL editors** — build scripts, report macros, and the admin SQL console (vendored, theme-aware)
- **Tag system** — 32-colour palette for organising datamodules
- **Dark/light mode** — PicoCSS with theme toggle
- **Admin panel** — system stats, configuration editor, read-only SQL console with schema sidebar, and external-connection manager

## Stack

- Python 3.14, Flask, APSW (SQLite for metadata), DuckDB (build + query engine)
- HTMX + PicoCSS + CodeMirror (all vendored, no frontend build step)
- [Gatekeeper](../gatekeeper/) client for SSO authentication
- [Outbox](../outbox/) client for email delivery (used by the future schedules phase)
- [webreports-caddy](../webreports-caddy/) (optional) reverse proxy when serving on a subpath
- openpyxl for XLSX export, pytz (DuckDB TIMESTAMPTZ results), cryptography (Fernet) for connection params at rest
- uv for dependency management, ruff + ty for code quality

## Concepts

### Datamodules, versions, and the atomic swap

A **datamodule** has a human path (e.g. `finance/monthly`), a DuckDB **build script**, and a TTL. Building runs the script into a scratch file, `CHECKPOINT`s it, and atomically publishes it as an immutable snapshot; a single SQLite transaction then marks the version ready and flips the datamodule's live pointer. In-flight readers keep their own file handle, so a rebuild never disturbs them.

```
DATA_ROOT/<uuid[:2]>/<uuid>/
    versions/<version_uuid>.duckdb   # immutable snapshots, one per successful build
    sources/<stored_name>            # uploaded build inputs (Excel/CSV/Parquet/…)
    tmp/<version_uuid>.duckdb        # in-progress build (same filesystem → atomic rename)
```

"Live" is only a pointer in the SQLite metadata DB (`datamodule.live_version_id`) — renaming or moving a datamodule is pure metadata, and old versions stay browsable until retention prunes them.

**Freshness**: each serve checks the TTL (report override falls back to the datamodule's). Blank = never auto-expire; `0` = rebuild on every request; a `valid_until` date forces staleness after a deadline. Stale requests rebuild inline under a single-flight lock, then serve the new version.

### Build inputs

- **Source files** — upload on the datamodule page, reference in the script as `source('sales.xlsx')`; it expands to the on-disk path as a SQL string literal (e.g. inside `read_xlsx(...)`).
- **Dependencies** — other datamodules ATTACHed read-only by a local name before the script runs. Each dependency is TTL-refreshed first, so pipelines are just dependencies with different TTLs. Cycles are rejected at add time.
- **Connections** — encrypted connection records ATTACHed into the build: SQLite and PostgreSQL become schemas named after the local name (`warehouse.dbo.orders`); ODBC connections become a session handle used as `odbc_query(getvariable('conn_<name>'), 'SELECT …')`.

### Reports

A **report** is a DuckDB table macro over its datamodule, stored in SQLite and applied as a `TEMP` macro at serve time — editing the body is instant, no rebuild. Bodies reference declared arguments with `$name`:

```sql
SELECT * FROM sales
WHERE region = $region
  AND order_date >= $from_date
```

Arguments have a type (`text`/`integer`/`number`/`decimal`/`date`/`time`/`boolean`), a widget (`input`/`select`/`multiselect`), an optional default, a required flag, and optionally a **constraint query** run against the live data to populate select options. A multiselect binds a DuckDB LIST — filter with `list_contains($picks, col)`. (Internally macro parameters are prefixed `arg_`, so an argument named after a column can never self-compare.)

A report body and a constraint query must each be **exactly one SELECT statement** (`WITH …`, `FROM …`, `VALUES`, and `UNION` all count; wrap a bare `PIVOT` as `SELECT * FROM (PIVOT …)`). Anything else is refused when the report is saved and again at serve time.

### The DuckDB sandbox

Every DuckDB connection that runs author SQL is locked down once its trusted setup is done (`services/duck.py`), using DuckDB's own `enable_external_access = false` + `allowed_directories` + `lock_configuration`:

- **Serving** (report bodies, constraint queries) — the live version file is ATTACHed read-only first; the SQL can then reach only that data. File reads, `COPY`, `ATTACH`, `INSTALL`/`LOAD`, and changing any setting are refused. Exports are written under `DATA_ROOT/_exports/` and spills under `DATA_ROOT/_tmp/`.
- **Builds** — dependencies and connections are attached first; the script can then read only its own datamodule's `sources/` (and write its `tmp/`). The `excel` extension is preloaded so `read_xlsx(source('…'))` keeps working; other extensions must be loaded by a bound connection's driver.
- **Escape hatch** — set `build.sandbox = false` in `/admin/config` if a build that worked before fails with a `Permission Error` (e.g. a connection driver that turns out to need external access, or a script that reads from an `https://` URL). Serving is always sandboxed.
- **Session options** — a build script may still `SET TimeZone`, `threads`, `memory_limit`, `storage_compatibility_version` and the like; DuckDB itself refuses to turn external access back on or widen the allowed directories.
- **Known gap** — once the `sqlite_scanner` extension is loaded (only for a build that binds a SQLite connection), DuckDB's SQLite reader does not apply the directory restriction, so that build can open other SQLite files.
- **Known DuckDB bug** — `enable_logging(storage='file', …)` pointed outside the allowed directories, followed by any query, aborts the whole process. A report body that does this crashes the web worker serving it (gunicorn restarts it).

## Serving reference

```
GET /r/<report_uuid>.<ext>?<args>&<grammar>     ext ∈ html | parquet | csv | json | xlsx
Auth: session (SSO) — or, for the data formats only, ?key=<api key token>
```

Query parameters:

| Form | Meaning |
|---|---|
| `name=value` | A declared argument (by name). If the name isn't an argument but matches an output column, it's an equality filter. |
| `col__op=value` | Filter on an output column. Ops: `eq ne gt gte lt lte in between contains isnull`. `in`/`between` take comma-separated values; `contains` is case-insensitive; `isnull` takes true/false. |
| `q=text` | Substring search across **all** columns (case-insensitive). Repeat `q=` to AND multiple fragments. |
| `sort=-a,b` | Sort by `a` descending then `b` ascending, NULLs last. |
| `page=N` | 1-based page of 200 rows. HTML is always paginated; exports return the full filtered result unless `page` is given explicitly. |
| `version=<version_uuid>` | Pin a retained historical version (never rebuilds; the page shows a banner with a back-to-live link). |
| `key=<token>` | API-key authentication (data formats only — see below). |

Every filter/sort field is validated against the query's real output columns (`DESCRIBE`) and values are always bound as parameters — nothing user-supplied is interpolated into SQL. Unknown query keys are an error (400 on data formats, an inline message on HTML). A single `_` in a hand-typed field name matches a column with a space (`net_sales` → `net sales`) when no exact column matches. `sort`, `page`, `q`, `format`, `key`, and `version` are reserved and rejected as argument names.

The HTML view adds sortable column headers, a search-all-columns box, removable chips for URL-carried filters, a pager, and per-format export links that preserve the current arguments/filters/sort.

## API keys (Excel / PowerQuery)

PowerQuery can't drive SSO, so **API keys** grant unauthenticated access to the *data* formats (never HTML):

- Created on the datamodule page: a name, a scope (the whole datamodule or a single report), optional **fixed arguments** (always win over caller values — one `name=value` per line, repeat a name for multiselect), an optional **whitelist** of caller-suppliable argument names (blank = all), and an optional expiry date.
- Only the sha256 of the token is stored. The plaintext token — and ready-made absolute PowerQuery URLs per report — are shown **exactly once**, at create or rotate time, with copy buttons. Paste the URL straight into Excel (Get Data → From Web); Parquet is the default format, and `.csv`/`.json`/`.xlsx` work by swapping the extension.
- Filters, sort, search, and version pins all compose with a key; keys can be rotated (breaks existing workbooks), disabled, and deleted.
- Every keyed request is access-logged (format, row count, bytes, duration, status, remote address), and the key's use count / last-used timestamp update on success. Invalid, disabled, expired, or out-of-scope tokens all get a uniform 403; keyless anonymous data requests get 401.

## Versions and retention

The datamodule page lists every retained version (status, duration, size, storage-format floor, trigger) with a **live** badge and per-report links that open the report pinned to that version. Retention is per-datamodule:

- **count** — keep the newest N versions (the live one is always kept), or
- **age** — keep versions younger than a window (blank = keep everything).

Pruning runs automatically after each successful build and deletes both the version row and its file. For everything retention can't reach, run:

```bash
strata-admin reconcile --dry-run    # report what would be removed
strata-admin reconcile              # prune + sweep
```

Reconcile enforces retention across all datamodules and sweeps orphaned datamodule directories (left behind by deletes), stale `tmp/` files from crashed builds, and version files with no database row. A live version whose file is missing is only ever warned about.

## Local development

```bash
# Install dependencies
make sync

# Create / migrate the local dev DB at instance/strata.sqlite3
make init-db

# Run the dev server (127.0.0.1:5000, debug mode)
make rundev
```

Strata can run standalone in dev — gatekeeper is optional. Without gatekeeper configured, authentication is disabled and there's no admin path. To wire up local SSO:

```bash
export GATEKEEPER_DB=/abs/path/to/gatekeeper/instance/gatekeeper.sqlite3
make rundev
```

The dev server picks up env vars on launch; restart after changing them.

## Production deployment with Docker

This is the full path from "fresh git clone on a server" to "logged-in admin building datamodules". Each numbered step depends on the ones above it.

### 1. Prerequisites

- Docker Engine and Compose v2 (`docker compose version` ≥ 2).
- A `platform-net` Docker network (shared by strata, gatekeeper, outbox):
  ```bash
  docker network create platform-net
  ```
- Sibling checkouts of [`gatekeeper`](../gatekeeper/) and [`outbox`](../outbox/). By default strata expects them at `../gatekeeper` and `../outbox` relative to its own directory; the paths are overridable via `GATEKEEPER_INSTANCE` and `OUTBOX_INSTANCE`.
- (Optional) [`webreports-caddy`](../webreports-caddy/) if you want to serve strata behind a reverse proxy at a subpath like `/strata`.

### 2. Bring up the sibling services first

Strata can start without them but won't be useful (no auth). Follow each project's README:

```bash
cd ../gatekeeper && make rebuild && docker compose up -d
cd ../outbox     && make rebuild && docker compose up -d
cd ../strata
```

### 3. Pin host paths with a `docker-compose.override.yml`

The base `docker-compose.yml` defaults to local paths under `./instance/`. For a real deployment you typically want the SQLite metadata DB and the (potentially large) datamodule storage on persistent disks. `docker-compose.override.yml` is gitignored and is the right place for site-specific values:

```yaml
# docker-compose.override.yml — production paths as an example
services:
  app:
    volumes:
      - /mnt/data/webreports_data/strata:/app/instance
      - /mnt/data/strata_data:/data-root
      - /mnt/data/webreports_data/gatekeeper:/gatekeeper-data
      - /mnt/data/webreports_data/outbox:/outbox-data
    ports:
      - "5005:5005"
    environment:
      - PORT=5005
      - HOST=0.0.0.0
```

### 4. Build images and apply schema migrations

```bash
docker compose build
make db-init        # creates instance/strata.sqlite3 if absent, applies migrations
docker compose up -d
```

`make db-init` runs `strata-admin init-db` inside a throwaway container based on the *just-built* `app` image, so the binary applying migrations is always in sync with what the long-lived container will run.

If you start `app` against a DB whose `schema_version` is older than the code expects (e.g. you forgot `make db-init` after a `git pull`), the app aborts at boot with a clear message pointing back at `make db-init`. You won't accidentally serve traffic on a broken schema.

### 5. Bootstrap your first admin

Strata's admin gate reads the gatekeeper user's group memberships. The default admin group is `strata-admins` (overridable via the `STRATA_ADMIN_GROUP` env var). Until at least one user is in that group, nobody can reach `/admin/*` or author datamodules.

In gatekeeper's web UI:

1. Log in as a gatekeeper admin.
2. Groups → **New** → name it `strata-admins` → save.
3. Groups → `strata-admins` → **Members** → add the gatekeeper user accounts who should manage strata.

Then log in to strata. The **Admin** link will be in the top nav. From `/admin` you can reach:

- **SQL Console** — read-only SELECT/PRAGMA/EXPLAIN against the metadata DB, with a schema sidebar.
- **Configuration** — DB-stored settings registry (`server.port`, `data.root`, etc.).
- **Connections** — register external databases for datamodule builds to ATTACH. See *Connecting via ODBC* below for the MSSQL setup.

### 6. After every `git pull`

```bash
git pull
make rebuild        # docker compose build + make db-init
docker compose up -d
```

`make rebuild` is the "one button" recipe — it rebuilds images and applies any new schema migrations in the right order.

### 7. Reverse-proxy notes

Strata's compose file ships with `PROXY_X_FORWARDED_PREFIX=1` (and the other three `PROXY_X_FORWARDED_*` flags) so `url_for()` produces correct URLs when fronted at a subpath. If you front strata with Caddy at `/strata`, make sure the proxy strips the prefix and sends `X-Forwarded-Prefix: /strata`. The reference setup is [`webreports-caddy`](../webreports-caddy/), whose Caddyfile contains a working `handle_path /strata/* { reverse_proxy strata-app:5005 { header_up X-Forwarded-Prefix /strata } }` block.

Standalone deployments (no proxy) can leave the env vars at `1` — ProxyFix on a non-proxied request is a no-op because the headers simply aren't present. The proxy flags also make the copy/paste PowerQuery URLs come out with the correct external scheme/host/prefix.

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

The string is Fernet-encrypted before it lands in the SQLite metadata DB; on Test, strata installs `odbc_scanner` (DuckDB core extension, v1.5.2+), runs `LOAD odbc_scanner`, and confirms the connection is reachable via `odbc_connect`. Bind the connection to a datamodule with a local name, then query it in the build script:

```sql
CREATE TABLE orders AS
SELECT * FROM odbc_query(getvariable('conn_navi'),
                         'SELECT OrderID, CustID, Total FROM dbo.Orders');
```

The inner SELECT executes on the remote SQL Server and only the result comes back to DuckDB; the rest of the build (joins with dependencies, transformations) runs in DuckDB. For PostgreSQL there's no need for ODBC — the dedicated `postgres` driver ATTACHes via DuckDB's native postgres extension and is faster.

If you need Always Encrypted, MARS, or AD-password auth with UPN UIDs (`user@domain.com`), add Microsoft's `msodbcsql18` back to the runtime stage of the `Dockerfile` (see the comment block in there). Bring your own apt-repo workaround — Microsoft's signing key was still SHA1-self-signed at the time we last looked, which Debian 12's apt now rejects.

## Configuration reference

Every setting strata reads is in the registry at `src/strata/config.py`. The runtime resolves each one in priority order: **env var > DB-stored value (`app_setting` table) > registry default**.

`/admin/config` shows the full registry, the *effective* value for each row, and which layer it came from (`env` / `db` / `default`). Env-overridden rows are read-only — to change them, edit the env var (typically in `docker-compose.yml`). All other rows are editable; saving writes to the DB layer. Each registry entry is also reachable from the CLI:

```bash
make config-list
make config-set KEY=auth.admin_group VAL=strata-admins
```

DB-only settings worth knowing (no env var; restart the app after changing the `server.*` ones):

| Registry key | Default | Purpose |
|---|---|---|
| `server.threads` | `8` | Request threads per gunicorn worker. Strata runs gunicorn's `gthread` worker, so a long export or build is not killed by the worker timeout. |
| `server.timeout` | `120` | Seconds a worker may go silent before gunicorn restarts it (a liveness check, not a per-request limit). |
| `build.sandbox` | `true` | Lock build scripts to their own `sources/` and `tmp/` (see *The DuckDB sandbox*). |

### Environment variables and registry mapping

| Variable | Registry key | Default | Purpose | Where set |
|---|---|---|---|---|
| `HOST` | `server.host` | `0.0.0.0` | Bind address for gunicorn / Flask. | Compose `app` |
| `PORT` | `server.port` | `5000` | Listen port; also drives the host port mapping. | Compose `app` |
| `STRATA_DB` | `storage.strata_db` | `/app/instance/strata.sqlite3` | Path to the SQLite metadata DB. **Bootstrap-only** — read before the DB is open, so it must come from the env. | Compose (commented) |
| `STRATA_ROOT` | `storage.strata_root` | (auto) | Project root used at init-db time to resolve `database/migrations/`. **Bootstrap-only.** | Compose (commented) |
| `STRATA_DATA_DIR` | (host-only) | `./instance/data` | Host directory mounted at `/data-root`. Pin to a dedicated disk — built versions can grow large. | Compose (host substitution) |
| `DATA_ROOT` | `data.root` | `/data-root` | Root for datamodule storage (versions + sources) inside the container. | Compose `app` |
| `GATEKEEPER_INSTANCE` | (host-only) | `../gatekeeper/instance` | Host path mounted at `/gatekeeper-data`. | Compose (host substitution) |
| `GATEKEEPER_DB` | `gatekeeper.db_path` | `/gatekeeper-data/gatekeeper.sqlite3` | **Required for SSO.** Path to gatekeeper's SQLite. | Compose `app` |
| `STRATA_ADMIN_GROUP` | `auth.admin_group` | `strata-admins` | **Required to access `/admin/*`.** Gatekeeper group for strata admins. | Compose `app` |
| `OUTBOX_INSTANCE` | (host-only) | `../outbox/instance` | Host path mounted at `/outbox-data`. | Compose (host substitution) |
| `OUTBOX_DB` | `outbox.db_path` | `/outbox-data/outbox.sqlite3` | Path to outbox's SQLite (email delivery — used by the future schedules phase). | Compose `app` |
| `MAIL_SENDER` | `mail.sender` | `strata@localhost` | From-address on outgoing emails (future schedules phase). | Compose (commented) |
| `PROXY_X_FORWARDED_FOR` | `proxy.x_forwarded_for` | `1` | Trust hop count for `X-Forwarded-For`. | Compose `app` |
| `PROXY_X_FORWARDED_PROTO` | `proxy.x_forwarded_proto` | `1` | Trust hop count for `X-Forwarded-Proto`. | Compose `app` |
| `PROXY_X_FORWARDED_HOST` | `proxy.x_forwarded_host` | `1` | Trust hop count for `X-Forwarded-Host`. | Compose `app` |
| `PROXY_X_FORWARDED_PREFIX` | `proxy.x_forwarded_prefix` | `1` | **Required behind a subpath proxy** for correct `url_for()` URLs. | Compose `app` |
| `TZ` | (container TZ) | `UTC` | Container timezone. UI still renders timestamps in the user's local TZ. | Compose `app` |

The host-only entries (`STRATA_DATA_DIR`, `GATEKEEPER_INSTANCE`, `OUTBOX_INSTANCE`) are bind-mount source paths consumed by `docker-compose.yml`; they don't reach the application. `worker.poll_interval` remains in the registry for the future schedules phase.

## Admin CLI

```bash
strata-admin init-db                  # create / migrate the metadata DB
strata-admin config list|get|set|export
strata-admin reconcile [--dry-run]    # retention + orphaned-file sweep
```

## Project structure

```
strata/
├── src/strata/
│   ├── __init__.py              # App factory (config resolution, Jinja filters, ProxyFix)
│   ├── web.py                   # Web entry point
│   ├── cli.py                   # Admin CLI (Click)
│   ├── config.py                # Typed config registry
│   ├── db.py                    # APSW SQLite connections + forward-only migration runner
│   ├── blueprints/
│   │   ├── auth.py              # Gatekeeper SSO
│   │   ├── datamodules.py       # Datamodule + report authoring, builds, API keys
│   │   ├── reports.py           # Public serve path /r/<uuid>.<ext>
│   │   ├── connections.py       # External-DB connection manager (admin)
│   │   ├── admin.py             # Admin panel, config, SQL console
│   │   └── tags.py              # Tag management
│   ├── models/                  # Dataclass models with static CRUD methods
│   ├── services/                # datamodule build/prune, report serve, query grammar, streaming
│   ├── static/                  # CSS, JS, vendored htmx/pico/codemirror
│   └── templates/               # Jinja2 HTML templates
├── database/
│   └── migrations/              # Forward-only NNNN_*.sql, applied by strata-admin init-db
├── pyproject.toml
├── Makefile
├── Dockerfile
├── docker-compose.yml
└── wsgi.py
```

## Make targets

| Target | Description |
|---|---|
| `make sync` | Install dependencies with uv (local dev) |
| `make init-db` | Create / migrate the local dev DB |
| `make rundev` | Run dev server (debug mode) |
| `make run` | Run production server (gunicorn) |
| `make check` | `lint` + `test` (run before every commit) |
| `make lint` | ruff format + ruff check + ty check |
| `make test` | Run the pytest suite |
| `make config-list` | Show all DB-stored configuration |
| `make config-set` | Set a DB-stored config value |
| `make rebuild` | After `git pull`: rebuild images + apply DB migrations |
| `make db-init` | Apply DB migrations against the running deployment |
| `make docker-up` | `docker compose up -d` |
| `make docker-down` | `docker compose down` |
| `make clean` | Remove temp files and the local dev DB |
