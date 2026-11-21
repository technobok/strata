# Strata

A reporting system built around **datamodules**: persistent, versioned DuckDB databases built by SQL scripts, with parameterised reports served as DuckDB macros over the live version — as interactive HTML or streamed Parquet/CSV/JSON/XLSX, including unauthenticated access for Excel/PowerQuery via hashed API keys.

## Features

- **Datamodules** — a DuckDB database built by a plain SQL script; every successful build is an immutable, versioned snapshot; "live" flips atomically, so readers are never disturbed
- **TTL freshness** — within the TTL a request serves the current version; past it, a rebuild is queued and the request waits briefly for it, else serves the previous version marked stale (the serve path never builds; blank TTL = never expire, `0` = always rebuild)
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
- **Admin panel** — system stats, configuration editor (live settings apply without a restart), read-only SQL console with schema sidebar, and external-connection manager
- **In-app help** — `docs/*.md` rendered at `/help`, with a contextual **?** next to each editor

## Stack

- Python 3.14, Flask, APSW (SQLite for metadata), DuckDB (build + query engine)
- HTMX + PicoCSS + CodeMirror (all vendored, no frontend build step)
- [Gatekeeper](../gatekeeper/) client for SSO authentication
- [Outbox](../outbox/) client for email delivery (used by the future schedules phase)
- [webreports-caddy](../webreports-caddy/) (optional) reverse proxy when serving on a subpath
- openpyxl for XLSX export, pytz (DuckDB TIMESTAMPTZ results), cryptography (Fernet) for connection params at rest
- uv for dependency management, ruff + ty for code quality

## Using Strata

How to write build scripts and reports, the URL grammar, API keys for Excel, freshness and schedules: all of it is in the in-app help (**Help** in the top bar, at `/help`, with a **?** next to each editor). The same pages are in [`docs/`](docs/) and read fine on GitHub:

| Page | Covers |
|---|---|
| [How Strata fits together](docs/index.md) | Datamodules, versions, reports, the worker; a diagram |
| [Build scripts](docs/build-scripts.md) | `source()`, dependencies, connections and `odbc_query`, session options, the sandbox |
| [Reports](docs/reports.md) | The single-SELECT body, `$arguments`, widgets, constraint queries |
| [Report URLs](docs/urls.md) | Filters (`col__op=v`), `q`, `sort`, `page`, `version`, `key`, response headers |
| [API keys and Excel](docs/api-keys.md) | Keys, fixed args, PowerQuery |
| [Freshness and builds](docs/freshness.md) | TTL, the stale-read wait, back-off, the worker, retention |
| [Schedules](docs/schedules.md) | The schedule kinds and custom RRULEs |
| [Settings (admin)](docs/settings.md) | Every setting and when a change applies |

The original design rationale is in [`docs/design/future-direction.md`](docs/design/future-direction.md); the plan of record is [`ROADMAP.md`](ROADMAP.md).

### Storage layout

A datamodule's files live under `DATA_ROOT` (pin it to a disk with room; built versions can grow large):

```
DATA_ROOT/<uuid[:2]>/<uuid>/
    versions/<version_uuid>.duckdb   # immutable snapshots, one per successful build
    sources/<stored_name>            # uploaded build inputs (Excel/CSV/Parquet/…)
    tmp/<version_uuid>.duckdb        # in-progress build (same filesystem → atomic rename)
```

"Live" is only a pointer in the SQLite metadata DB (`datamodule.live_version_id`); renaming a datamodule is pure metadata.

### Retention sweeps

Retention prunes old versions (rows and files) after each successful build. For everything it can't reach, run:

```bash
strata-admin reconcile --dry-run    # report what would be removed
strata-admin reconcile              # prune + sweep
```

Reconcile enforces retention across all datamodules and sweeps orphaned datamodule directories (left behind by deletes), stale `tmp/` files from crashed builds, and version files with no database row. A live version whose file is missing is only ever warned about.

### Sandbox notes for operators

Every DuckDB connection that runs author SQL is locked down once its trusted setup is done (`services/duck.py`: `enable_external_access = false` + `allowed_directories` + `lock_configuration`). Author-facing rules are in [Build scripts](docs/build-scripts.md#the-sandbox). Two known limits:

- **SQLite gap** — once the `sqlite_scanner` extension is loaded (only for a build that binds a SQLite connection), DuckDB's SQLite reader does not apply the directory restriction, so that build can open other SQLite files.
- **Known DuckDB bug** — `enable_logging(storage='file', …)` pointed outside the allowed directories, followed by any query, aborts the whole process. A report body that does this crashes the web worker serving it (gunicorn restarts it).

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

### 4. Build images and start

```bash
docker compose build
docker compose up -d    # starts both `app` (web) and `worker` (builds)
```

Both containers run `strata-admin init-db` before they start, which creates `instance/strata.sqlite3` if it is absent and applies any pending migrations. The migration runner is safe to run from both at once: each migration is applied exactly once.

The `worker` service runs `strata-worker` from the same image with the same volumes; without it, builds are queued but never run (the admin page and each datamodule page say so).

`make db-init` applies migrations without restarting anything: it runs `strata-admin init-db` in a throwaway container from the *just-built* `app` image, so the code applying migrations always matches what the containers will run.

If the app ever starts against a DB whose `schema_version` is older than the code expects (e.g. its command was overridden so the migration step did not run), it aborts at boot with a clear message pointing at `strata-admin init-db`. You won't accidentally serve traffic on a broken schema.

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
docker compose build
docker compose up -d    # recreates the containers; each migrates before it starts
```

`make rebuild` (build + `make db-init`) still works if you prefer to apply migrations before recreating the containers.

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

`/admin/config` shows the full registry, the *effective* value for each row, which layer it came from (`env` / `db` / `default`), and when a change takes effect. Env-overridden rows are read-only — to change them, edit the env var (typically in `docker-compose.yml`). All other rows are editable; saving writes to the DB layer.

The `build.*`, `serve.*`, `schedule.*` settings and `worker.poll_interval` **apply immediately**: they are read when used (cached for 5 seconds), so the web app uses a saved change at once and the worker within 5 seconds. The rest are read once at start, and each row says whether it needs the app, the worker, or both restarted. Each registry entry is also reachable from the CLI:

```bash
make config-list
make config-set KEY=auth.admin_group VAL=strata-admins
```

Every setting, its default and when a change applies: [Settings (admin)](docs/settings.md).

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

The host-only entries (`STRATA_DATA_DIR`, `GATEKEEPER_INSTANCE`, `OUTBOX_INSTANCE`) are bind-mount source paths consumed by `docker-compose.yml`; they don't reach the application. `worker.poll_interval` (default 30 s) is how often the worker sweeps for dead builds (due schedules are checked every 10 s regardless); `worker.concurrency` (default 2) is how many builds one worker runs at once.

## Admin CLI

```bash
strata-admin init-db                  # create / migrate the metadata DB
strata-admin config list|get|set|export
strata-admin reconcile [--dry-run]    # retention + orphaned-file sweep + dead builds
strata-worker [--queue Q]... [--concurrency N] [--once]   # run queued builds
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
│   │   ├── help.py              # In-app help: docs/*.md at /help
│   │   └── tags.py              # Tag management
│   ├── models/                  # Dataclass models with static CRUD methods
│   ├── services/                # datamodule build/prune, report serve, query grammar, streaming
│   ├── static/                  # CSS, JS, vendored htmx/pico/codemirror
│   └── templates/               # Jinja2 HTML templates
├── database/
│   └── migrations/              # Forward-only NNNN_*.sql, applied by strata-admin init-db
├── docs/                        # In-app help pages (rendered at /help); design/ = history
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
| `make rebuild` | Rebuild images + apply DB migrations (the containers also migrate on start) |
| `make db-init` | Apply DB migrations without restarting the containers |
| `make docker-up` | `docker compose up -d` |
| `make docker-down` | `docker compose down` |
| `make clean` | Remove temp files and the local dev DB |
