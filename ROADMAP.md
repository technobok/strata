# Strata Roadmap

Strata pivoted from a report-centric tool to **datamodule as core** (design doc:
`doc/future-direction.md`). The v1 cut line — a complete streaming, secured
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
- **Exception reports mail on change.** A check declares key columns; each run is diffed
  against the last (new / still open / resolved) and mail goes out only when something
  changed, listing all open rows with an xlsx attachment and a live link.
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
| A | **Harden** | pytest suite; DuckDB sandbox (external access off, allowed directories, locked configuration); report bodies must be one SELECT; read-only SQL console; gthread gunicorn so long exports survive; bug fixes; pin duckdb. |
| B | **Build execution** | `strata-worker` with a SQLite job queue and leases (`build_token`/`build_lease_until`) and reaping of stuck builds; serve never builds (stale-while-revalidate); failure backoff; scheduler (`schedule` table + `services/schedule_service.py` date maths); push refresh of dependents; per-build resource caps; queues. |
| C | **Checks + exception reports** | Check object (gate / notify); per-company templating (parity with webreports' `{codb}`/`{cocode}`); new/open/resolved diffing; mail-on-change via Outbox; run history; exceptions overview page; heartbeat alert when a check stops running; importer for `testtest` SQL rows. |
| D | **Pilot migration** | Port 5–10 representative webreports checks (one per-company), run them alongside webreports and compare; then plan the batches. |
| E | **Data layer** | Incremental datamodules (previous version ATTACHed as `prev` + a watermark); lineage (row counts, dependency versions, content-addressed sources); catalog page + FTS (`datamodule_fts`); decide whether `syncdb` keeps feeding coredb. |
| F | **Presentation** | Formatting metadata (format, align, class whitelist, links to reports with an argument mapping; also drives xlsx number formats); KPI tiles + simple charts. |
| G | **Sensitive instance** | A second isolated deployment (own `DATA_ROOT` + DB + groups) for payroll-type data, when needed. |

## Backlog (unscheduled, carried forward)

- **API rate limiting** — throttle keyed endpoints; must also cover key-triggered TTL rebuilds (cost amplification).
- **Authorization-header token auth** — alternative to `?key=` so tokens stay out of proxy/server request logs.
- **Version keep/pin flag** — mark a specific version exempt from retention (e.g. year-end snapshots).
- **Editor assists** — DuckDB `sql_auto_complete()` completion, `$arg` name completion, EXPLAIN preview for build scripts/macros.
- **Row/time caps on serving** — guardrails for pathological queries.
- **Operations** — metrics endpoint, structured JSON logging, backup CLI (SQLite + data root).
- **Holiday table** — schedule business-day awareness beyond Mon–Fri (feeds Phase B).
- **API keys admin view** — global list across datamodules + per-key access-log browser.
- **Serving niceties** — `page_size` parameter, multi-column sort from the UI (grammar already supports it).
- **tom-select** — vendored but unwired; wire it (e.g. multiselect args, tag pickers) or drop it.
