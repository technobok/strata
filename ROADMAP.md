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

## Next phases

| # | Phase | Goal |
|---|---|---|
| 7 | **Formatting views (safe HTML)** | `format_view` + `format_column` column-by-column builder (tables exist in the schema); generated view macros emit inner-cell HTML through system escaping primitives (`strata_text/num/date/link`); the server owns the `<td>` wrapper + a CSS-class whitelist; golden XSS tests. |
| 8 | **Peripheral repoint** | Schedules retargeted at datamodules (`rebuild` pre-warm + `email` actions) with a **rewritten** worker (`services/schedule_service.py` next-run math is kept for this); FTS search screen (`datamodule_fts`); tag UI wiring; client library against `/r/<uuid>.<ext>`. |
| 9 | **Trust model + sensitive instance** | Author/runner group split (`author_required` vs `login_required`); site-level access gate; per-instance config; a second isolated deployment (own `DATA_ROOT` + DB + groups) for sensitive data; documented per-build resource caps. |
| 10 | **Dashboards & charts** | View variants emitting hero tiles or ECharts specs; grid dashboards composing sub-reports with shared args; vendored `echarts.min.js`. |

## Backlog (unscheduled, carried forward)

- **API rate limiting** — throttle keyed endpoints; must also cover key-triggered TTL rebuilds (cost amplification).
- **Authorization-header token auth** — alternative to `?key=` so tokens stay out of proxy/server request logs.
- **Version keep/pin flag** — mark a specific version exempt from retention (e.g. year-end snapshots).
- **Editor assists** — DuckDB `sql_auto_complete()` completion, `$arg` name completion, EXPLAIN preview for build scripts/macros.
- **Per-build resource caps** — `memory_limit`, `threads`, `max_temp_directory_size`, statement timeout on build connections (Phase 9 documents; this implements).
- **Row/time caps on serving** — guardrails for pathological queries.
- **Operations** — metrics endpoint, structured JSON logging, backup CLI (SQLite + data root).
- **Holiday table** — schedule business-day awareness beyond Mon–Fri (feeds Phase 8).
- **Exception-report mode** — deliver scheduled output only when the result is non-empty (feeds Phase 8).
- **API keys admin view** — global list across datamodules + per-key access-log browser.
- **Serving niceties** — `page_size` parameter, multi-column sort from the UI (grammar already supports it).
- **tom-select** — vendored but unwired; wire it (e.g. multiselect args, tag pickers) or drop it.
