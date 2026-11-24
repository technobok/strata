# Checks and runs

A **check** is a report that looks for problems: rows it finds are exceptions,
such as stock below zero, an order with no customer, or a price that changed by
more than half. Any report can be a check. Tick **This is a check** on the report's
page and choose a **severity**.

```sql
-- Negative stock
SELECT company, item, on_hand
FROM stock
WHERE on_hand < 0
ORDER BY company, item
```

No rows means all is well. Write the check so that each row is one thing someone
should look at.

## Severity

| Severity | Use it for |
|---|---|
| `info` | Worth knowing; nobody needs to act today. |
| `warning` | Needs a look soon. The default. |
| `error` | Wrong data that needs fixing now. |

The severity shows on the datamodule's report list and on every run of the check.

## Gate checks

A check marked **Gate** also runs **inside every build** of its datamodule. It runs
after the build script, on the new data, before that data goes live:

- **No rows:** the build finishes and the new version goes live as usual.
- **Rows:** the build fails with "check '…' found N row(s): …" (the first rows are
  shown). The previous version stays live, so reports keep serving the last good
  data. The usual back-off for failed builds applies (see
  [Freshness and builds](freshness.md#failed-builds)).
- **The check itself fails** (for example, it names a column the new data no longer
  has): the build fails too. A gate that cannot run never lets data through.

A gate runs with its arguments' **defaults**, because nobody is there to fill them
in. A required argument without a default is refused when you tick Gate.

Use a gate for problems that make the data unusable, such as missing companies, an
empty extract or duplicated keys, where yesterday's correct data is better than
today's broken data.

## The exceptions overview

**Checks** in the top bar lists every check, flagged ones first. A check is
**flagged** when its latest run found rows. For each check it shows:

- its severity, and whether it is a gate;
- its last run (open it to see the rows) and how many rows that found;
- when it runs next, from its enabled schedules;
- how current its datamodule's data is: **fresh**, **stale** (past its TTL),
  **failing** (the last build failed; hover for the error) or **never built**.
  A check can only be as current as the data it reads.

Filter by severity, or show only flagged checks.

## Runs

A **run** executes a report or check once and saves every row it returned, so you
can look at that result later however the data changes. Each run records:

- when it ran, who ran it, and with which arguments;
- how old the data was ("data as of"), and a note if it may be out of date;
- the row count, or the error if the query failed.

**Run now** on the report's page runs it with the default arguments. **Save a run**
on the report itself runs it with the arguments on screen. Either opens the run's
page.

The run page shows the saved rows with the same sorting, search, filters and
paging as a report, and downloads them as Parquet, CSV, JSON or XLSX. **View
current** opens the live report with the same arguments, to compare with now.

The report's page lists its last 20 runs.

### Out-of-date data

A run uses the newest data it can get. If the datamodule is past its TTL, the run
waits for the rebuild like a report read does. If that rebuild fails, or is still
running when the wait ends, the run uses the previous version and says so on the
run page, with the reason.

### How long runs are kept

Runs are kept for `runs.retention_days` days (default 30); then the run and its
saved rows are deleted. Deleting a report deletes its runs.

## Scheduled runs and mail

A report or check can have any number of **schedules** of its own (the
**Schedules** section of the report's page). Each says **when** to run (the same
rules as datamodule schedules, see [Schedules](schedules.md)), **with which
arguments**, and **who gets the mail**. One check can have "BRS every Monday 07:00
to `@brs-finance`" and "OTA on the last day of the month to `jsmith`", each with its
own `company` value.

**Recipients**, one per line:

| Line | Who |
|---|---|
| `jsmith` | the Gatekeeper user `jsmith` |
| `@brs-finance` | every member of the Gatekeeper group `brs-finance` |
| `ops@example.com` | that address |

Type to get suggestions from Gatekeeper (see [Access](access.md#owners-and-viewers)).
Saving refuses a user or group Gatekeeper does not know; an email address is taken
as it is. A user who later leaves, is disabled, or has no email address is skipped
when mail is sent; the run page says who was skipped and why.

**Who gets mail:**

| The run | Mail |
|---|---|
| A **check** that finds rows | to the schedule's recipients, every time |
| A **check** that finds no rows | none |
| A plain **report** | to the schedule's recipients, every time, even with no rows |
| A run whose **query failed**, or whose datamodule is locked | to the datamodule's alert list only, with the error (no rows) |

A check mails on **every** run that finds rows, not only when something changed:
the same exception mails again until it is fixed.

**The mail** has:

- a subject such as `[WARNING] Negative stock — 7 rows` (a plain report has no
  severity);
- a first line with how old the data is, and why if it may be out of date, e.g.
  "Data as of Fri 21 Nov 2026 06:45 NZDT; refresh failed: …" (for a restricted
  datamodule, without the error);
- the first 20 rows as a table, unless the datamodule is restricted (its viewers
  are not everyone): then only the count, and the rows stay in Strata (see
  [Access](access.md#mail-from-a-restricted-datamodule));
- a link to the saved run, with every row, sorting and downloads. The link needs
  the `server.public_url` setting.

There is no attachment: the run page has the downloads.

When a schedule is due, the worker runs the report: if its datamodule is past its
TTL, it rebuilds it first. If that rebuild fails, the run goes ahead on the last
good data and the mail's first line says so. The run page shows what happened to
the mail.
