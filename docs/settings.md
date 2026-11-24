# Settings (admin)

**Admin → Configuration** lists every setting, its value, where the value comes
from, and when a change takes effect.

## Reading the page

**Source** says where the value comes from:

| Source | Meaning |
|---|---|
| `env` | An environment variable sets it (usually in `docker-compose.yml`). The row is read-only; change the variable to change it. |
| `db` | Saved on this page or with `strata-admin config set`. **Clear** goes back to the default. |
| `default` | Nothing is set; the built-in default applies. |

**Takes effect** says what a change needs:

| Label | Meaning |
|---|---|
| applies immediately | The web app uses it at once; the worker within 5 seconds. An environment variable still wins over the saved value. |
| restart the app | Restart `strata-web` (the `app` container). |
| restart the worker | Restart `strata-worker` (the `worker` container). |
| restart app + worker | Restart both. |

After **Save Configuration**, the message lists what changed, grouped by when it
applies. A blank field always means "back to the default", here and from the shell.

From a shell, the same settings are available as:

```bash
strata-admin config list
strata-admin config set build.memory_limit 6GB    # prints when it applies
```

## Builds — apply immediately

| Setting | Default | Meaning |
|---|---|---|
| `build.memory_limit` | `4GB` | DuckDB memory per build; it spills to disk beyond this. Two builds at once plus the web app fit a 16 GB host. A script may `SET memory_limit` itself. |
| `build.memory_duckdb_default` | off | Tick to use DuckDB's own default memory limit (80% of RAM) for builds; `build.memory_limit` is then ignored. |
| `build.threads` | `0` | DuckDB threads per build; `0` = one per core. |
| `build.timeout_seconds` | `3600` | A build still running after this long is stopped and fails; the previous version stays live. `0` = no limit. |
| `build.sandbox` | on | Locks build scripts to their own files (see [Build scripts](build-scripts.md#the-sandbox)). Turn off only if a connection driver fails under the sandbox. |

## Serving — apply immediately

| Setting | Default | Meaning |
|---|---|---|
| `serve.max_wait_seconds` | `60` | How long a read of stale data waits for the rebuild before serving the previous version (see [Freshness and builds](freshness.md#stale-data)). Keep it under Excel's 100 seconds. |
| `serve.memory_limit` | `2GB` | DuckDB memory per report query; it spills to disk beyond this. |
| `serve.memory_duckdb_default` | off | Tick to use DuckDB's own default memory limit (80% of RAM) for report queries; `serve.memory_limit` is then ignored. |

## Runs and mail — apply immediately

| Setting | Default | Meaning |
|---|---|---|
| `server.public_url` | — | The address readers use to reach Strata, e.g. `https://reports.example.com/strata`. Links in mail are built from it; the admin page warns while it is unset. |
| `mail.sender` | `strata@localhost` | The From address on mail Strata sends (env `MAIL_SENDER`). |
| `outbox.db_path` | — | Outbox's SQLite file, which queues outgoing mail (env `OUTBOX_DB`). |
| `outbox.url` | — | Outbox's HTTP API, used when `outbox.db_path` is not set. |
| `outbox.api_key` | — | The key for `outbox.url`. |
| `runs.retention_days` | `30` | Days a report or check run, with its saved rows, is kept (see [Checks and runs](checks.md#how-long-runs-are-kept)). The worker's sweep and `strata-admin reconcile` delete older ones. |

## Read audit — apply immediately

| Setting | Default | Meaning |
|---|---|---|
| `audit.retention_days` | `365` | Days a row of the [read audit](audit.md) is kept. The worker's sweep and `strata-admin reconcile` delete older ones. `0` keeps them all. |

## Diagnostics — apply immediately

| Setting | Default | Meaning |
|---|---|---|
| `diag.auth_probe` | off | Turns on `/diag/auth-probe.json`, which answers anyone (no login) with what their request carried: for testing how Excel gets through the sign-in in front of Strata (see [Testing Excel's sign-in](api-keys.md#testing-excels-sign-in-the-auth-probe)). Turn it off after the test. |

## Access — apply immediately

Who may open and edit a datamodule is set on its page. These settings are the lists a
datamodule follows until it has lists of its own. A list holds usernames (as Gatekeeper
has them, e.g. `asiap\jsmith`), `@groups`, and `*` for everyone signed in.

| Setting | Default | Meaning |
|---|---|---|
| `auth.writer_group` | `strata-writers` | The Gatekeeper group whose members may create datamodules. Admins may too. |
| `access.default_owners` | — | The owners a new datamodule's form starts with; blank = the writers group (`@` + `auth.writer_group`). Owners edit the datamodule, its reports, schedules and access. Changing it does not change existing datamodules. |
| `access.default_viewers` | `*` | The viewers a new datamodule's form starts with; `*` = everyone signed in. Viewers open its reports and runs. Changing it does not change existing datamodules. |

## Schedules and the worker

| Setting | Default | Takes effect | Meaning |
|---|---|---|---|
| `schedule.timezones` | `Australia/Brisbane, Australia/Sydney, Pacific/Auckland` | immediately | Timezones offered for schedules; the first is the default. |
| `worker.poll_interval` | `30` | immediately | Seconds between the worker's clean-up sweeps (dead builds, abandoned jobs). Due schedules are checked every 10 seconds regardless. |
| `worker.concurrency` | `2` | restart the worker | Builds one worker runs at once. `strata-worker --concurrency N` overrides it. |

## Web server — restart the app

| Setting | Default | Meaning |
|---|---|---|
| `server.host` | `0.0.0.0` | Address the production server listens on (env `HOST`). |
| `server.port` | `5000` | Port (env `PORT`). |
| `server.dev_host` | `127.0.0.1` | Address for `strata-web --dev`. |
| `server.dev_port` | `5000` | Port for `strata-web --dev`. |
| `server.debug` | off | Flask debug mode. |
| `server.threads` | `8` | Request threads per gunicorn worker. |
| `server.timeout` | `120` | Seconds a gunicorn worker may go silent before it is restarted. It is a liveness check, not a limit on a request. |
| `server.max_upload_mb` | `512` | Largest upload (source files); larger gets HTTP 413. |
| `proxy.x_forwarded_for` | `0` | Trust `X-Forwarded-For` from this many proxies (env `PROXY_X_FORWARDED_FOR`). |
| `proxy.x_forwarded_proto` | `0` | Trust `X-Forwarded-Proto` (env `PROXY_X_FORWARDED_PROTO`). |
| `proxy.x_forwarded_host` | `0` | Trust `X-Forwarded-Host` (env `PROXY_X_FORWARDED_HOST`). |
| `proxy.x_forwarded_prefix` | `0` | Trust `X-Forwarded-Prefix`; needed behind a proxy at a subpath such as `/strata` (env `PROXY_X_FORWARDED_PREFIX`). |
| `auth.admin_group` | `strata-admins` | The Gatekeeper group whose members are Strata admins (env `STRATA_ADMIN_GROUP`). |

## Services and storage — restart app + worker

| Setting | Default | Meaning |
|---|---|---|
| `data.root` | `instance/data` | Where datamodule versions and uploaded files live (env `DATA_ROOT`). |
| `gatekeeper.db_path` | — | Gatekeeper's SQLite file, for single sign-on (env `GATEKEEPER_DB`). |
| `gatekeeper.url` | — | Gatekeeper's HTTP API, instead of the file. |
| `gatekeeper.api_key` | — | The key for `gatekeeper.url`. |
| `storage.strata_db` | — | The Strata metadata database. Set only by the env variable `STRATA_DB`, because it is needed before the database opens. |
| `storage.strata_root` | — | The project root, used to find migrations and these help pages. Set only by the env variable `STRATA_ROOT`. |
