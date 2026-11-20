# Potential future design direction for strata

> **Status (2026-07):** this is the design document that drove the pivot; the
> core of it — Phases 1–6, through commit `b5225d0` — is **implemented**. Kept
> as-is for the rationale. Deliberate deviations from the text below:
>
> - There is **no live `data.db` file**: "live" is a SQLite pointer
>   (`datamodule.live_version_id`) to an immutable `versions/<uuid>.duckdb`,
>   and storage is sharded as `DATA_ROOT/<uuid[:2]>/<uuid>/{versions,sources,tmp}/`.
> - API keys are **sha256-hashed bearer tokens** (datamodule- or report-scoped,
>   with fixed/whitelisted args and a one-time reveal), not stored GUIDs.
> - Per-report ACLs were dropped in favour of the per-instance isolation model
>   (ROADMAP Phase G).
> - The serve grammar was extended beyond the sketch: `q=` search,
>   `between`/`isnull` ops, `version=` pinning, `key=` auth.
>
> **Superseded (2026-11-20, see `ROADMAP.md` "Direction"):** the "Formatting
> Reports" section below is replaced by per-column formatting metadata rendered
> by the server — DuckDB never builds `<td>` HTML. The dashboard ideas (grid
> layouts, sub-report composition) are dropped in favour of Excel/PowerQuery
> plus light KPI tiles and charts. Scheduled exception reports, a background
> worker, and incremental builds are the next priorities (Phases A–E).

## Architecture

- SQLite db is used to store metadata

- change from 'cache' to 'data' in the config file. It is no longer just a
  cache

## Components

### Datamodules (the core abstraction)

- instead of 'report' as the core object as in the current app, a 'datamodule'
  should be the core.

- the datamodule names are required to be unique. The datamodule name should be
  a valid path using forward slashes to separate so a hierarchy can be created
  that will be mirrored on disk. Non-standard pathname characters must be
  forbidden (only allow, a-z, A-Z, 0-9, ",.-_+=()" and space (others?)). Webapp
  has the ability to rename datamodules which will move the directories and
  data for the report and will update metadata (likely in sqlite). Need to be
  able to handle (retry?) if the datamodule is currently in use / locked.

- datamodules can also be 'tagged' with user definable tag pills to allow
  non-hierachical tagging to assist with search. Each datamodule should have a
  free text description as well that can be searched (FTS5) in a search screen.

- the datamodule path name will be a directory where data related to the report
  is stored. Contents of that directory:
  - data.db - the duckdb database for this report (most recent version)
  - data - subdirectory where uploaded source data (optionally) is stored for
    eg list of Excel files/CSV/Parquet that will be read into the duckdb file
    (like for payroll reports) - concatenated into a single table.
  - version - subdirectory where duckdb database files for previous runs are
    (optionally) stored. Will have GUID's as names and the metadata for each
    will need to be stored in SQLite. Need to keep SQLite db and filesystem in
    sync.

- the web interface should have a "build" text box for a datamodule that has
  the creation sql script that will build the data.db for the current report.
  The build script cannot take any arguments. It is given an empty duckdb file
  and is run inside that context. It may have some fixed information passed
  into it (eg. path of "data" folder so that those files within can be read
  into a table). The system will also run the ddl sql for the "reports" macros
  (see next point) so that the datamodule is fully (re)created from scratch
  each run.

### Data Reports (0 or more per Datamodule)

- data "reports" are the duckdb macros that actually return data to the user
  via a url (to browser/excel-powerquery)

- from the web interface, you can view, delete, create the associated "reports"
  for the datamodule. The system will add/remove macros to the duckdb database
  to back each report (add to the current report during development, but also
  appended to the build step when doing a full refresh of the datamodule). 


- Data reports can have arguments, see below

- The body of the macro should just be a textbox where the body sql can be
  entered.

- each "report" has an output format: parquet (streaming), excel, html (others
  in the future? csv). Specified by the user in the builder

- the "reports" are accessible via a http url

- Need to be able to access from Excel (via Powerquery). Need an 'API key' that
  is randomly generated GUID per report and can be regenerated to break the
  connection with existing excel workbooks if required. A valid API key will
  allow unauthenticated access (from HTTP point of view; bypasses Gatekeeper
  auth) to the data since powerquery does not easily support http auth. The
  full URL (including API key and parameters with placeholders) to be used in
  Powerquery can be generated on the report page for easy copy/paste into
  powerquery. For Powerquery the file type will usually be a streaming parquet
  file format straight from duckdb. Perhaps use a compressed or truncated
  version of the GUID -- not sure if powerquery has a URL length limit. The URL
  should point to a specific datamodule+report.

- there should be 0 or more GUID API keys to allow different report access for
  different uses. Would need to be able to create and delete (revoke) each
  separately. Need an interface for this. Good for logging access as well. The
  API key should have a human readable name that can be entered for each.

### Data Report arguments

- The web interface will allow 0 or more arguments to be created for each
  report. The name, data type, default value and constraint must be specified
  for each argument. The parameter constraint can be a query that runs within
  the duckdb db and returns name,description records for that argument - name
  is a value that can be selected for that argument and description is a human
  readable description for that name. Multiple select can be allowed (argument
  then takes an array?). 

Report argument types?

- text (min/max length?)
- select/multiselect
- number
- date (date range?)
- time

### Formatting Reports (0 or more per Datamodule)

- formatting "reports" are also duckdb macros and are similar to data reports
  except they are backed by a data report (or table/view?) and cannot have
  their own arguments (the have the same arguments as the underlying data
  report) and the UI helps to build/format each field, one by one.

- also accessible via url for browser and powerquery (same as data reports)

- the system will have a way to help create/update/delete formatting macros
  that can format the output of each columns for display in html somewhat
  similar to Excel's formatting: number of decimals, thousands separators, date
  formatting, left/right justification, css class, links to other reports with
  arguments etc. Duckdb itself will create the whole "td" element text to be
  output for each field. The interface should allow the report designer to
  select a macro, then list each of the fields returned and their types, and
  then have a selectable format dropdown (for common formats like 2 decimal
  number format, common date formats, text (left/right justified), None - to
  skip output of that column) but also have an option to have custom sql to
  format the field. 

- There should be a number of standard CSS classes that are available to
  control formatting (justification, coloring etc) - set in settings panel ?.
  The result when confirmed should be added to the duckdb as a macro
  immediately, but also added when rerunning the datamodule. The macro created
  should have the same parameters as the underlying macro. The user needs to
  select the underlying macro to format when starting a formatting macro. These
  should be presented in a dropdown. The system needs to be able to examine the
  fields that will be returned so it can present the columns for formatting in
  the interface. The user can select each field that they want in a dropdown -
  each field can be used 0 (skipped) or more times (multiple formats of the
  same underlying column). Also need to have a free form column where the user
  can make their own sql (for combining columns or generating data for a
  column.) For each column, the name that will be returned can be set (defaults
  to underlying column name if any), then a format from the dropdown can be
  chosen or custom then an sql fragment entered. Or maybe, always custom sql
  but the dropdown allows prefilling the sql that can be used as a starting
  expression that can be edited?

- we also need a way to specify links to other reports in the columns/cells?
  Need a way to build these in the duckdb view-builder as well somehow.
  Building link will require a way to refer to the target report and may need
  to access multiple fields to build a link with arguments. Custom sql but have
  some prespecified variables that can be used to get url of target report?

- could formatting reports return other items other than tables? eg. a hero box
  with a single value, or a charting component (echarts). That is, the building
  blocks for a complete html dashboard? Can have a report that can layout (grid
  layout?) a number of other sub-reports? Would need to be able to show
  argument specifiers in the report with default values, that could be pushed
  down to the subcomponents to keep the page in sync. Could have a static
  minilanguage that can do the grid page layout?

- charting? Make dashboard widgets like Superset? Are these separate 'reports'
  or 'widgets' is a separate table/functionality? Can just be a normal
  formatting report?

- what are the options for showing the duckdb source properly syntax
  highlighted (prism?). what are the options for syntax highlighting and error
  checking in a textbox when writing the 'build' script or the macro body?
  Possible ideas:
    - CodeMirror vs Monaco others? Seems codemirror might be a better match. 
    - Consider duckdb's sql_auto_complete() function to assist with
      autocomplete? 
    - Consider DuckDB-WASM?

### Sorting and filtering returned data

- data and formatting reports can be sorted and filtered before returning the data

- sorting and filtering http GET query strings
A compact, parseable scheme that's worked well in lots of APIs:
/r/sales?region=North&revenue__gte=1000&product__in=A,B,C&sort=-revenue,region&page=2

Filters as `field` or `field__op=value`. Bare `field=value` means equality; the `__op`
suffix carries the operator. A small fixed operator vocabulary covers nearly
everything: eq, ne, gt, gte, lt, lte, in, between, contains, isnull. The
double-underscore style is convenient because an htmx form input can literally
be named `revenue__gte` and serialize itself.
Sort as `sort=-revenue,region` — comma-separated, leading `-` for descending,
multi-key in order. (Borrowed from JSON:API/DRF; compact and unambiguous.)

- need to ensure the sorting and filtering fields do not clash with any
  parameter fields that the report mugh have

- field names may be for display so could have spaces. Need to be able to
  handle that from the url side - maybe accept a single underscore in the url
  but convert to space to resolve to field name? Then we couldn't have actual
  underscores in the field name though. UI would need to prevent that.

### Versions and Time-to-live

- for old report versions - the datamodule must specify keeping X runs or keep
  all runs within a time window. We will need a background job to remove old
  reports and update the metadata periodically. Need to be able to browse to
  the older versions as well and view their reports over that data.

- the reports need a TTL (time to live) parameter. If the persisted data is
  within the TTL (ie. before the expiry date/time) then data is returned from
  that cached data. If it is not, the report should be rebuilt before returning
  the data. "ephemeral" reports have a short TTL like 60seconds. Manually
  refreshed reports have a very long TTL or a date far into the future. Values
  for TTL:
  - X (seconds, days, or a fixed date) - valid until that many seconds/days
    after last refresh or valid until specified date.

- then can have a core report with a very short TTL like 60s then can have a
  'cache' separate report that points to source report but with a longer TTL.
  If required.

- can have a 'pipeline' that is then built in reverse - last report in the
  pipeline can have a long TTL then each source report in the chain can have a
  short(er) TTL so that when parent refreshes, they all pull fresh data

- allow scheduled update of a report, cron background worker(s) ???


### Datamodule metadata

- store date run inside the duckdb file. And other metadata (metadata table)
        - date started
        - date ended
        - copy of the build query that was run?
    - How long it took
    - who triggered it (username, guid, cronjob?)

### Datamodule supporting files

- data directory: Need ability to upload, download, delete, 0 or more files for
  the data directory that can be used in the build step

### Dependancies

- need to be able to have dependencies (specified as a user defined name (valid
  label only, no spaces or strange characters) and a typeahead/dropdown of
  other datamodules in the system). The report text can then refer to these and
  before running, the system can poke/refresh those reports to ensure they are
  current (inside their TTL). Before running the build, the system should
  attach these dependancies so they can be referred to in the body of the build
  by the user specified name


### Authentication / Authorization

- gatekeeper integration for authentication

- Authorization: need to totally lock out some users from some reports eg payroll
  reports but excel external data access should still work (up to the users not
  to share the reports). Needs to prevent building or running a child report
  that hits this as well.

- or should we have a totally separate run time instance (separate docker
  process) to run the sensitive reports and fully lock out most people (via
  gatekeeper) so they cannot access the site at all?


## Overview

1. create new duckdb file and run build sql
2. data "report" macros (returns data, optionally has parameters which the interface
   helps to build) - body is sql text
3. formatting "report" (also macros, but the web interface helps to build them
   column by column. Can't do joins, can only refer to existing
   tables/views/macros). Body is put together by the system (from the
   field-by-field setup created with the web ui).

