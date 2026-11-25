# Import and export

A **definition file** describes one datamodule in text: its script, settings,
access lists, tags, connections, dependencies, reports, checks and schedules.
`strata-admin import` makes or updates datamodules from these files, and
`strata-admin export` writes them out. Use them to:

- create many datamodules at once (the webreports testrunner migration does);
- keep a copy of your datamodules in git, and review a change as a diff;
- copy work from a test Strata to the live one.

Only admins run these commands, on the server. Nothing here is in the web pages.

## A file

A file is [TOML](https://toml.io). SQL goes between `'''` exactly as written, with
no escaping, and so does a name like `'asiap\pawe'` in single quotes.

```toml
path = "checks/baditemdepartment"
description = "Items with a DEPARTMENT dimension (webreports test 129)"
tags = ["testrunner"]
ttl_seconds = 900
script = '''
CREATE TABLE result AS
SELECT * FROM odbc('navibrs_ro', 'select No_, Description from [Audmet Australia Pty_ Ltd_$Item]');
'''

[connections]          # local name = Strata connection name
navibrs_ro = "navibrs_ro"

[dependencies]         # local name = datamodule path
companies = "ref/companies"

[[report]]
name = "Each Item must NOT have a DEPARTMENT dimension"
macro = "baditemdepartment"
description = '''
No item may have a DEPARTMENT default dimension. Remove it on the item card.
'''
body = "SELECT * FROM result"
check = "error"        # info, warning or error; leave it out for a plain report
gate = false
subtotals_by = []      # how it is shown: see "Formatting" below
total_row = false
style = []

[[report.column]]      # one per column with settings
name = "No_"
link = "/nav?co=BRS&item={No_}"
new_tab = true

[[report.arg]]         # one per argument, in order
name = "company"
type = "text"          # text, integer, number, decimal, date, time, boolean
widget = "input"       # input, select, multiselect
required = false
default = "BEAU"

[[report.schedule]]
name = "workdaily"
kind = "daily"
fields = { times = "17:00", days = ["MO", "TU", "WE", "TH", "FR"] }
timezone = "Australia/Brisbane"
recipients = ['asiap\pawe', 'masterdataau']
```

### What each key does

| Key | Meaning |
|---|---|
| `path` | Required. Which datamodule this is: an existing one is updated, otherwise one is made. |
| `description`, `script` | As in the datamodule's form. |
| `tags` | Its [tags](index.md#the-pieces), all of them. A tag that does not exist yet is made. |
| `ttl_seconds` | Seconds, or `"never"`. See [Freshness](freshness.md#ttl). |
| `retention_kind`, `retention_count`, `retention_age_seconds` | `"count"` or `"age"`; a number; seconds or `"all"`. |
| `build_queue` | The worker queue. |
| `owners`, `viewers`, `alerts` | Its [access lists](access.md#owners-and-viewers) and alert list. |
| `stale_alert_hours` | Hours, or `"off"`. |
| `[connections]` | Connections to bind, `local name = connection name`. The connection must exist, and the owners must be on its "May use" list. |
| `[dependencies]` | Datamodules to depend on, `local name = path`. |
| `[[schedule]]` | A rebuild schedule of the datamodule. |
| `[[report]]` | A report: `name` and `body` are required. `macro` is used when the report is made and must match after that. |
| `[[report.arg]]` | An argument: `name`, `type`, `widget`, `required`, `default`, `constraint` (a SELECT), `description`. |
| `[[report.schedule]]` | A run-and-mail schedule: `name`, `kind`, `fields`, `timezone`, `enabled`, `recipients`, `parameters`. |
| `[[report.column]]` | How one result column is shown: `name` (required), `format` (an Excel format code), `align` (`auto`, `left`, `center`, `right`), `total` (`sum`, `avg`, `min`, `max`, `count`, `count_distinct`), `hidden`, `link` (a template such as `https://…?p={Item}`), `link_text`, `new_tab`. A key left out of one is its default. `column = []`: no column has settings. |
| `[[report.style]]` | A style rule: `when` (a condition on the row, e.g. `"Amount" < 0`), `columns` (`[]` = the whole row), `style` (e.g. `"red bold"`). `style = []`: none. |
| `subtotals_by`, `total_row` | Subtotal rows per value of these columns, in order; a total row at the end. Both need a column with a `total`. |

A schedule's `kind` and `fields` are the schedule form's, so it supports exactly what
the form does ([Schedules](schedules.md)):

| `kind` | `fields` |
|---|---|
| `every` | `every_n`, `every_unit` (`minutes` or `hours`), `days`, `window_start`, `window_end` |
| `daily` | `times` (e.g. `"07:00, 13:30"`), `days` |
| `weekly` | `every_weeks`, `days`, `time`, `start_date` |
| `monthly` | `every_months`, `day` (a number or `"last"`), `time` |
| `working_day` | `which` (`first` or `last`), `time` |
| `once` | `at` |
| `rrule` | `rrule`, `start_date` |

`days` is a list such as `["MO", "TU"]`; none means every day. `timezone` defaults
to the first of the `schedule.timezones` setting.

A key that is left out is left alone: a new datamodule gets the defaults, as the
form gives them, and an existing one keeps what it has. A key Strata does not know
refuses the file, so a typo is never silently ignored.

### Formatting

A report's `column`, `style`, `subtotals_by` and `total_row` keys are how its rows are
shown; they never change its data. Each one the file gives replaces that part of the
report's settings, and each one it leaves out is kept: a file of only reports' SQL
leaves their formatting alone. A setting that does not fit the report refuses the
file, saying what and where, e.g. a format for dates on a number column, or a column
the report does not have. Before the datamodule has data (a new one, not built yet)
only what needs no data is checked: the column names and types are not known yet.

## Importing

```
strata-admin import --dry-run FILES-OR-FOLDERS...
strata-admin import FILES-OR-FOLDERS...
```

A folder means every `*.toml` file in it and below it. In the containers, put the
files under `instance/` on the host; the app sees them under `/app/instance`:

```
docker compose exec app strata-admin import --dry-run /app/instance/import
docker compose exec app strata-admin import --as 'asiap\pawe' /app/instance/import
```

The rules:

- **Matching:** a datamodule by `path`, a report by `name` (ignoring case) within
  its datamodule, and a schedule by `name` within its report.
- **The same checks as the forms.** A report must run on the datamodule's live data
  when there is any; a gate check's arguments need defaults; lists name people
  Gatekeeper knows; the owners must be able to read each dependency and use each
  connection.
- **Each file is all or nothing.** A refused file changes nothing, its reason is
  printed, and the next file is still tried. The command exits with 1 if any file
  was refused.
- **What a file leaves out stays.** A report you added in the web form is kept when
  the file does not list it. `--prune` removes the reports, schedules, dependencies
  and connection bindings a file does not list. A removed report's run files are
  swept by `strata-admin reconcile`.
- **The file wins for what it lists.** If someone edited a listed report in the web
  form, importing puts the file's version back. A dry run shows it first
  (`update … report 'X'`).
- **Recipients who have left.** A schedule recipient Gatekeeper does not know is left
  out, with a note. An unknown owner, viewer or alert-list name refuses the file,
  because dropping it would change who may read the data or hears about problems.
- **A changed script makes the data stale**, as in the form, so the next read or run
  rebuilds it. The reports in the same file are not checked against the old data.
- **Files that depend on each other** are imported in order: a datamodule's
  dependencies before it.

Options:

| Option | Does |
|---|---|
| `--dry-run` | Checks every file and prints what would change; changes nothing. One difference from a real run: a dependency on a datamodule an earlier file makes is noted, not checked. |
| `--prune` | Also removes what a file does not list (see above). |
| `--build` | Queues a build of each datamodule made or changed, so a broken script shows up now instead of at its first scheduled run. Its failure is not mailed (a manual build's never is). |
| `--only 'checks/*'` | Only the files whose `path` matches. |
| `--as NAME` | Recorded as who made or changed them (default `strata-admin`). |

Each line of output is one datamodule: `create`, `update` (with what changed),
`unchanged`, or `REFUSED` with the reason. Notes follow on indented lines.

## Exporting

```
strata-admin export --out FOLDER [--only 'checks/*']
```

Writes one file per datamodule, its path as folders: `checks/baditemdepartment`
goes to `FOLDER/checks/baditemdepartment.toml`. Every key is written, so the file
imports back unchanged. Not in a file: the data and its versions, runs, API keys,
the Excel link (a new datamodule gets one, as in the form), uploaded source files
and the read audit.
