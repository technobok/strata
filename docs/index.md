# How Strata fits together

Strata turns SQL into reports. An author writes a **build script** that pulls data
from source systems into a **datamodule** (a DuckDB database). **Reports** are
SELECT queries over a datamodule, served as web pages and as data files that Excel
can refresh.

```
  Connections ──┐  SQL Server (ODBC), PostgreSQL, SQLite
  Source files ─┼─▶ Build script ──▶ Version ──▶ live
  Dependencies ─┘   (DuckDB SQL)     v1 v2 v3     (the version reports read)
                                                   │
                                     Reports: SELECT queries with $arguments
                                                   │
                  ┌────────────────────────────────┼─────────────────────────┐
                  ▼                                ▼                         ▼
          /r/<id>.html                 /r/<id>.parquet .csv          Excel / PowerQuery
          browser, logged in           .json .xlsx                   ?key=<API key>

  strata-worker  runs every build (queued by the Build button, a schedule, or a
                 report read of stale data) and fires schedules.
  strata-web     serves pages and data; it never builds.
```

## The pieces

**Datamodule.** A named DuckDB database such as `finance/monthly`. It has a build
script, optional inputs, a TTL and a retention policy. See
[Build scripts](build-scripts.md).

**Build and version.** Each successful build writes a new, immutable **version**
and then makes it **live** in one step. Readers already using the old version keep
it, so a build never disturbs a report that is running. Old versions stay
available until retention removes them. See [Freshness and builds](freshness.md).

**Inputs.** A build script can read:

- uploaded **source files** (Excel, CSV, Parquet, …);
- other datamodules, as **dependencies**;
- external databases, through **connections** that an admin sets up.

**Report.** A single SELECT over the datamodule, with typed `$arguments` the
reader fills in. Editing a report needs no rebuild. See [Reports](reports.md).

**Check.** A report that looks for problems: rows it finds are exceptions. A
**gate** check also runs inside every build and stops bad data from going live.
See [Checks and runs](checks.md).

**Run.** One execution of a report or check, with its rows saved so the result can
be looked at later. See [Checks and runs](checks.md#runs).

**Report URL.** Every report is served at `/r/<id>.<format>`. The URL also carries
filters, sorting, search and paging. See [Report URLs](urls.md).

**API key.** A token that lets Excel or PowerQuery fetch a report's data without
logging in. See [API keys and Excel](api-keys.md).

**Schedule.** A recurrence rule that rebuilds a datamodule ahead of time, for
example every weekday at 06:00. See [Schedules](schedules.md).

## Freshness in one paragraph

A datamodule's **TTL** is how old its data may get. When a report is read and the
data is older than the TTL, Strata asks the worker for a rebuild. The report waits a
short while for it, and otherwise serves the previous version marked as stale. A
build that fails leaves the previous version live, and the datamodule's alert list
hears about it. See [Freshness and builds](freshness.md#alerts).

## Who does what

- **Owners** of a datamodule write its build script and reports in SQL. **Writers**
  (the `strata-writers` group) create datamodules and own what they create.
- **Viewers** of a datamodule open its reports in the browser or refresh them in
  Excel. The home page lists every report you can open.
- **Admins** can do everything, and also manage connections and settings. See
  [Access](access.md) and [Settings](settings.md).
