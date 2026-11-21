# Strata Roadmap

Strata pivoted from a report-centric tool to **datamodule as core** (design doc:
`docs/design/future-direction.md`). The v1 cut line — a complete streaming, secured
data-API + tabular-report product — shipped as Phases 1–6.

## Shipped — v1 (Phases 1–6)

| Phase | Commit | What landed |
|---|---|---|
| 0 | `f74caeb` | Forward-only migration runner + `0001_baseline.sql` (the datamodule schema) |
| 1 | `14525e3` | Walking skeleton: build → CHECKPOINT → atomic swap → versioned live; reports as TEMP macros at `/r/<uuid>.{html,parquet}`; TTL rebuild with single-flight; authoring UI |
| 2 | `25cb131` | Build inputs: uploaded source files + `source('name')`; dependencies ATTACHed by local name (TTL pull, cycle rejection); external connections via `attach_into` |
| 3 | `afd5dc3` | Argument builder: all types incl. `time`; input/select/multiselect widgets; constraint-query option pickers; multiselect → DuckDB LIST; macro params prefixed `arg_` |
| 4 | `385b3a6` | Serve grammar (`field__op`, `q=`, `sort=-a,b`, `page=`) validated against DESCRIBE columns; CSV/JSON/XLSX streaming; HTMX sortable headers, search box, filter chips, pager |
| 5 | `019aaf5` | Version pinning (`?version=`); retention prune (count/age) + post-build hook; `strata-admin reconcile`; true storage-format recording; versions UI; vendored CodeMirror editors |
| 6 | `b5225d0` | Hashed API keys (`?key=`, data formats only) with fixed/whitelisted args, access log, one-time token reveal + PowerQuery URLs |

## Direction (decided 2026-11-20)

Strata replaces the **reporting core** of webreports: the `testtest`/`cronserver`
exception checks, the data preparation (`syncdb`, `rprtsales`) and the page reports.
Jobs that change data (move/rebalance/EDI/fixit import files) stay outside Strata;
Strata is read-only on its sources.

- **Authors** are IT only, writing SQL. The UI stays SQL-first; the DuckDB sandbox is
  defence in depth, and there is no author/runner group split.
- **Checks** are one primitive with two uses: a *gate* (rows fail the build before the
  swap, so the old version stays live) or *notify* (a scheduled exception report).
- **Exception reports notify whenever they find rows** (revised 2026-11-21: diffing
  against the last run was judged too fragile). Reports and checks have 0..n schedules of
  their own; each scheduled run saves a snapshot and mails its recipients the row count,
  the first 20 rows and a link to the snapshot — no xlsx attachments. A check with no rows
  sends nothing; a failed query alerts only the datamodule's alert list (admins).
- **Freshness is pushed**: the scheduler and upstream completion enqueue builds; the serve
  path never builds and shows when the data was built.
- **BI** stays in Excel/PowerQuery. Strata adds only KPI tiles and simple charts; the
  dashboard composer (old Phase 10) is dropped.
- **Formatting** is per-column metadata rendered by the server with autoescaping. DuckDB
  never emits HTML; the old Phase 7 column-builder design is dropped.

## Next phases

These replace the old Phases 7–10.

| # | Phase | Goal |
|---|---|---|
| A | **Harden** — done 2026-11-20 (`d510d43`..`18e0a8b`) | pytest suite (`make check` = lint + test); DuckDB sandbox (`services/duck.py`); report bodies and constraint queries must be one SELECT; read-only SQL console; gthread gunicorn so long exports survive; manual builds take the build lock; reconcile reaps dead builds; bug fixes; duckdb pinned `>=1.5.2,<2`. |
| B | **Build execution** — done 2026-11-21 (`9f4eb53`..`049a4c3`) | `strata-worker` + `build_job` queue (coalescing, atomic claims, queues, concurrency) and a cross-process build lease; the serve path never builds (60 s bounded wait, then the last good version marked stale; 503 if never built); failure backoff (1 min doubling to 1 h); "Build with all dependencies" (a plain build refreshes only stale dependencies — dependents are never pushed); RRULE schedules in configurable timezones; per-build/per-query memory limits and a build timeout. |
| C | **Help + checks + scheduled reports** | Auto-migrate on start; live settings; in-app help at `/help` (docs/*.md, contextual links); checks = reports with severity + optional gate (rows fail the build before the swap); 0..n schedules per report/check with recipients (users, `@groups`, emails); run snapshots (Parquet, 30 days) with a run page; notification mail (count, 20 rows, link); failure/recovery + staleness alerts; Jinja per-company loops in build scripts; exceptions overview page. Plan: `~/dev/claude/plans/strata-phase-c-checks.md`. |
| D | **Pilot migration** | Port 5–10 representative webreports checks (one per-company), run them alongside webreports and compare; then plan the batches. |
| E | **Data layer** | Incremental datamodules (previous version ATTACHed as `prev` + a watermark); lineage (row counts, dependency versions, content-addressed sources); catalog page + FTS (`datamodule_fts`); decide whether `syncdb` keeps feeding coredb. |
| F | **Presentation** | Formatting metadata (format, align, class whitelist, links to reports with an argument mapping; also drives xlsx number formats); KPI tiles + simple charts. |
| G | **Sensitive instance** | A second isolated deployment (own `DATA_ROOT` + DB + groups) for payroll-type data, when needed. |

## Backlog (unscheduled, carried forward)

- **API rate limiting** — throttle keyed endpoints; must also cover key-triggered TTL rebuilds (cost amplification).
- **Authorization-header token auth** — alternative to `?key=` so tokens stay out of proxy/server request logs.
- **Version keep/pin flag** — mark a specific version exempt from retention (e.g. year-end snapshots).
- **Editor assists** — DuckDB `sql_auto_complete()` completion, `$arg` name completion, EXPLAIN preview for build scripts/macros.
- **Reproducible image** — the Dockerfile installs from `pyproject.toml` (not `uv.lock`) and pulls gatekeeper/outbox from git HEAD, so a rebuild picks up the newest versions within each constraint (`duckdb` is bounded `>=1.5.2,<2`). Use the lock instead: `uv export --frozen --no-dev` minus the two sibling path packages, installed from pinned git refs. Needs a test `docker compose build`.
- **Non-root container** — add a `USER` to the Dockerfile. Existing deployments' bind-mounted `instance/` and data root are root-owned, so this needs a one-time `chown` step in the deploy notes.
- **Row/time caps on serving** — guardrails for pathological queries.
- **Operations** — metrics endpoint, structured JSON logging, backup CLI (SQLite + data root).
- **Holiday table** — public holidays for schedules: "first/last working day" and business-hours rules currently mean Mon–Fri (RRULE EXDATE can exclude dates, but a holiday-shifted first working day needs code).
- **API keys admin view** — global list across datamodules + per-key access-log browser.
- **Serving niceties** — `page_size` parameter, multi-column sort from the UI (grammar already supports it).
- **tom-select** — vendored but unwired; wire it (e.g. multiselect args, tag pickers) or drop it.
