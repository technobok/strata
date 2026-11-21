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
