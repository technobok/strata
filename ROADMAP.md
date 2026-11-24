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

These replace the old Phases 7–10. **Order decided 2026-11-21: XL → G → D → E → F**
(plan: `~/dev/claude/plans/strata-excel-payroll.md`).

| # | Phase | Goal |
|---|---|---|
| A | **Harden** — done 2026-11-20 (`d510d43`..`18e0a8b`) | pytest suite (`make check` = lint + test); DuckDB sandbox (`services/duck.py`); report bodies and constraint queries must be one SELECT; read-only SQL console; gthread gunicorn so long exports survive; manual builds take the build lock; reconcile reaps dead builds; bug fixes; duckdb pinned `>=1.5.2,<2`. |
| B | **Build execution** — done 2026-11-21 (`9f4eb53`..`049a4c3`) | `strata-worker` + `build_job` queue (coalescing, atomic claims, queues, concurrency) and a cross-process build lease; the serve path never builds (60 s bounded wait, then the last good version marked stale; 503 if never built); failure backoff (1 min doubling to 1 h); "Build with all dependencies" (a plain build refreshes only stale dependencies — dependents are never pushed); RRULE schedules in configurable timezones; per-build/per-query memory limits and a build timeout. |
| C | **Help + checks + scheduled reports** — done 2026-11-21 (`a688c9d`..`205e8cc`) | Containers migrate on start (race-safe runner); live settings (build/serve/schedule/runs/mail apply without a restart; `/admin/config` says when each change applies); in-app help at `/help` from `docs/*.md` with contextual "?" links and drift tests; checks = reports with a severity and an optional gate (rows fail the build before the swap); saved runs (Parquet snapshots, 30 days) with a run page; 0..n schedules per report/check, each with its own recipients (users, `@groups`, emails) and arguments; notification mail through Outbox (count, data age + refresh problems, 20 rows, link; a check with no rows sends nothing); a refresh that fails still runs on the last good data and says so; per-datamodule alert lists (build failing / recovered / stale); Jinja loops in build scripts (`rows()`, `sqlstr`, `ident`); the `/checks` exceptions overview. Plan: `~/dev/claude/plans/strata-phase-c-checks.md`. Stage-gate smoke: `~/dev/claude/work/strata/smoke-c6/smoke.sh`. |
| XL | **Excel access** — done 2026-11-23 (X6: the unused report "Default format" and per-report TTL dropped, migration 0012); X0 (the auth probe) done 2026-11-21; X1 (2026-11-23): in the office Excel reaches Strata directly and anonymously, and Entra refuses Excel's Organizational-account token (WAM IncorrectConfiguration), so Excel links carry a key; X3+X4 done 2026-11-23: Excel links (`?key=`; since 2026-11-24 also an `Authorization: Bearer` header, tested in Excel from the office — the box stays one link, docs/api-keys.md says how to move a workbook to the header; `.xlsx` by default: Excel's From Web cannot open Parquet) — one Excel link per **datamodule** (migration 0011; shown in every report's "Use in Excel" box to anyone who can open the report, and in the datamodule page's Reports table; replaced only by the datamodule's owners and the admins, which also disables the older per-report links); admin keys always read the whole datamodule and keep their links on the datamodule page | Entra (IT's application proxy) pre-authenticates all of reportsau.demant.com and no app registration will be requested, so first an auth probe (`/diag/auth-probe.json`, with an opt-in `?challenge=<tenant>` that starts Excel's Organizational-account sign-in) and an Excel test decide what can work: Entra "Organizational account" sign-in if a token reaches Strata, otherwise personal API keys (any user's own key, stored encrypted so links can embed it). Every report page gets a "Use in Excel" link for the current view. Excel feeds replace direct MSSQL access, now denied to new users. Also: remove the unused report "Default format". |
| AC | **Access control** — done 2026-11-23 (`aa3128f`..AC6; plan: `~/dev/claude/plans/strata-access-control.md`; stage-gate smoke: `~/dev/claude/work/strata/smoke-ac/smoke.sh`). Datamodules made before AC are owned by the admins only, viewed by everyone; connections start admins-only; run mail from a restricted datamodule (viewers not `*`) carries the count and a link, no rows; the connection rule locks like the dependency rule; viewers' home is a Reports page. Read audit done 2026-11-24 (migration 0013, `/admin/reads`, each datamodule's Reads page). Next: D. | Datamodules are open by default and can be locked: per-datamodule **Owners** and **Viewers** (users or `@groups`), inherited by its reports, runs, checks and Excel links; roles admin / writer (new writers group) / viewer, with default lists from settings; non-members get 403; the dependency rule (each owner of a dependant must read every parent) is checked at every build, and a failure locks the datamodule (serves nothing) until an owner or admin fixes it; connections get a "may use" list; a cut-down interface for viewers. Payroll becomes a locked datamodule in this instance. |
| D | **Pilot migration** — in progress: `ref/companies` and pilot checks 129/127/161 ported by hand (2026-11-24); definition files + `strata-admin import`/`export` and datamodule tags done 2026-11-25 (help `docs/import-export.md`) for the bulk migration (plan `~/dev/claude/plans/strata-import.md`: every testrunner test except those that write, run Python code or have a fix-it) | Build `ref/companies` from coredb `xsyscompany`; port 5–10 representative webreports checks (at least one per-company, as a Jinja loop), schedule them with their webreports recipients, run them alongside webreports and compare; then plan the batches. Column-name formats (`FH_`/`F4_`/`UItemURL_`) wait for Phase F. |
| E | **Data layer** | Incremental datamodules (previous version ATTACHed as `prev` + a watermark); lineage (row counts, dependency versions, content-addressed sources); catalog page + FTS (`datamodule_fts`); decide whether `syncdb` keeps feeding coredb. |
| F | **Presentation** | Formatting metadata (format, align, class whitelist, links to reports with an argument mapping; also drives xlsx number formats); KPI tiles + simple charts. |
| G | ~~Sensitive instance~~ — dropped 2026-11-23 | Replaced by AC: payroll is a locked datamodule in the main instance, not a second deployment. Its read audit shipped 2026-11-24 and link-only mail for restricted data in AC6. |

## Backlog (unscheduled, carried forward)

- **HTMX on the datamodule page and the report form** (user, 2026-11-24: after the Phase D pilot) — today every section of the long datamodule page (build script, access, alerts, connections, dependencies, sources, API keys) and the report form is a full-page post + redirect, so each save reloads, jumps to the top and loses the editor's state. Each section becomes a partial that saves in place and shows its own message (as the connection form's driver fields and the Excel box already do).

- **API rate limiting** — throttle keyed endpoints; must also cover key-triggered TTL rebuilds (cost amplification).
- **CSRF tokens** — POST forms carry no token; Gatekeeper's `SameSite=Lax` cookie blocks cross-site posts but not posts from another `*.demant.com` site. A token in a `base.html` meta tag + `hx-headers` + the `post_button` macro would cover every form at once.
- **Version keep/pin flag** — mark a specific version exempt from retention (e.g. year-end snapshots).
- **Editor assists** — DuckDB `sql_auto_complete()` completion, `$arg` name completion, EXPLAIN preview for build scripts/macros.
- **Reproducible image** — the Dockerfile installs from `pyproject.toml` (not `uv.lock`) and pulls gatekeeper/outbox from git HEAD, so a rebuild picks up the newest versions within each constraint (`duckdb` is bounded `>=1.5.2,<2`). Use the lock instead: `uv export --frozen --no-dev` minus the two sibling path packages, installed from pinned git refs. Needs a test `docker compose build`.
- **Non-root container** — add a `USER` to the Dockerfile. Existing deployments' bind-mounted `instance/` and data root are root-owned, so this needs a one-time `chown` step in the deploy notes.
- **Row/time caps on serving** — guardrails for pathological queries.
- **Operations** — metrics endpoint, structured JSON logging, backup CLI (SQLite + data root).
- **Holiday table** — public holidays for schedules: "first/last working day" and business-hours rules currently mean Mon–Fri (RRULE EXDATE can exclude dates, but a holiday-shifted first working day needs code).
- **Serving niceties** — `page_size` parameter, multi-column sort from the UI (grammar already supports it).
- **tom-select** — vendored but unwired; wire it (e.g. multiselect args, tag pickers) or drop it.
- **DuckDB encryption at rest** — encrypt version files (key outside `DATA_ROOT`), e.g. for the payroll instance.
- **Run snapshots of gate failures** — a gate that fails a build reports its first 3 rows in the error; saving all its rows as a run would make them browsable.
