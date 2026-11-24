# Build scripts

A datamodule's **build script** is DuckDB SQL that runs top to bottom into a fresh,
empty database. Every table or view it creates becomes part of the new version.
Reports then read those tables.

```sql
CREATE TABLE sales AS
SELECT region, product, revenue, sold_on
FROM read_xlsx(source('sales.xlsx'));

CREATE TABLE totals AS
SELECT region, sum(revenue) AS revenue
FROM sales
GROUP BY region;
```

A script may hold any number of statements. If one fails, the build fails and the
previous version stays live.

The version keeps **only what the script creates**: name each table with
`CREATE TABLE <name> AS SELECT …` (or a view with `CREATE VIEW`), and reports read it by
that name. A `SELECT` on its own runs, but its rows are thrown away, so a script that
creates no table or view fails rather than leave an empty version.

Each version keeps the script it was built from: **script** in its row of the
datamodule's **Versions** table shows it (for a Jinja script, the rendered text that
ran).

## Source files

Upload files on the datamodule page (Excel, CSV, Parquet, JSON, …), then refer to
each one by name with `source('name')`. It expands to the file's path on disk, as a
SQL string literal:

```sql
CREATE TABLE stock AS SELECT * FROM read_csv(source('stock.csv'));
CREATE TABLE budget AS SELECT * FROM read_xlsx(source('budget 2027.xlsx'), sheet = 'NZ');
```

Uploading a file with the same name again replaces it; the next build reads the new
file.

## Dependencies

A datamodule can read another datamodule's live version. Add it under
**Dependencies** with a **local name**; the script then sees it as a read-only
database of that name:

```sql
CREATE TABLE nz_sales AS
SELECT * FROM sales_core.sales WHERE country = 'NZ';
```

Before the build runs, each dependency that is past its own TTL is rebuilt first.
So a pipeline is just a chain of dependencies with different TTLs. A dependency
cycle is refused when you add it.

**Build with all dependencies** (on the datamodule page) rebuilds every dependency
all the way down, each one once, and then this datamodule. Use it after fixing an
upstream script.

## Connections

An admin registers external databases under **Admin → Connections**. A datamodule
**binds** a connection under a local name (the **Connections** section of its page),
and its build script then reads it **by that name**; the page shows how, next to each
binding. A connection that is not bound to the datamodule does not exist for its
build, and reports never read connections (they read the built data):

- **SQLite and PostgreSQL** are attached as a database named after the local name,
  so tables are `name.schema.table`:

  ```sql
  CREATE TABLE orders AS SELECT * FROM warehouse.public.orders;
  ```

- **ODBC** (SQL Server through FreeTDS) is read with `odbc('<local name>', '<query>')`.
  Pass the remote query as text; it runs on the server, and only its result comes
  back:

  ```sql
  CREATE TABLE orders AS
  SELECT * FROM odbc('navi', 'SELECT OrderID, CustID, Total FROM dbo.Orders');
  ```

  Decimal columns (NAV's amounts and quantities) arrive exact, with their own
  precision. Three limits of SQL Server through FreeTDS:

  - **One SELECT per query.** A `WITH … SELECT` is fine; several statements are
    not (`DECLARE`, `SET NOCOUNT ON`, a temp table, `EXEC` of dynamic SQL): FreeTDS
    cannot describe their result. Rewrite such a query as one SELECT: a CTE instead
    of a temp table, an expression instead of a variable (`dateadd(day,
    datediff(day, 0, getdate()), 0)` instead of `@today`), a Jinja `{% if %}` or loop
    instead of dynamic SQL.
  - **One read of a connection per statement.** Two `odbc()` calls on the same
    connection in one statement (e.g. joined by `UNION ALL`) fail. Use a statement
    each (`CREATE TABLE … AS`, then `INSERT INTO … BY NAME`), or put the `UNION ALL`
    inside the SQL Server query.
  - **Use `odbc()`, not `odbc_query()` itself.** `odbc()` reads decimals the way that
    works; `odbc_query()` with its defaults fails on every decimal column, and a
    script may not set its `decimal_columns_*` options (one combination returns wrong
    numbers without an error).

  The handle itself is the variable `getvariable('<local name>')`.

Credentials are stored encrypted. The build script never sees them.

## Loops over rows (Jinja)

A build script that contains `{{` or `{%` is a **template**: Strata renders it with
[Jinja](https://jinja.palletsprojects.com/) just before it runs, so one script can
repeat a statement for every row of a dependency. The usual case is one branch per
company, from a `ref/companies` datamodule (added as a dependency named
`companies`):

```sql
{% for c in rows('companies', "SELECT shortname, companynamesql FROM companies ORDER BY shortname") %}
{% if loop.first %}CREATE TABLE stock AS{% else %}INSERT INTO stock BY NAME{% endif %}
SELECT {{ c.shortname | sqlstr }} AS company, *
FROM odbc('navi', {{ ("SELECT ItemNo, Qty FROM " ~ c.companynamesql ~ ".dbo.Stock") | sqlstr }});
{% endfor %}
```

Each company is a statement of its own because one connection can be read only once
per statement (see [Connections](#connections)). When companies live on different
servers, add the connection's local name to the companies' rows and read
`odbc({{ c.connection | sqlstr }}, …)`, binding each of those connections.

What a template can use:

- `rows('dependency', "SELECT …")`: the rows of one SELECT on that dependency (its
  version for this build), each a mapping of column name to value, e.g.
  `c.shortname`. At most 10,000 rows.
- `{{ value | sqlstr }}`: the value as a quoted SQL string: `O'Brien` →
  `'O''Brien'`; nothing → `NULL`.
- `{{ value | ident }}`: the value as a quoted SQL name: `net sales` →
  `"net sales"`.

Jinja's own `{% for %}`, `{% if %}`, `loop.first` / `loop.last`, `~` (join text)
and so on all work. A name that does not exist is an error, not an empty string.
Always pass values through `sqlstr` or `ident`; never paste them into SQL as they
are.

**Preview rendered script** on the datamodule page shows the SQL the template
produces, using the dependencies' live data. A build renders it again with the
versions it attaches, and the version keeps the **rendered** script, so you can
always see what really ran. A template error fails the build with its line, e.g.
"build script, line 3: 'c' is undefined".

## Session options

A script may `SET` DuckDB session options for its own build, for example:

```sql
SET TimeZone = 'Pacific/Auckland';
SET memory_limit = '6GB';   -- above the build.memory_limit default
SET threads = 1;
```

The defaults come from the `build.memory_limit` and `build.threads` settings (see
[Settings](settings.md)).

## The sandbox

Build scripts run in DuckDB's sandbox:

- A script can read only its own datamodule's uploaded files. It can write only to
  its own scratch directory.
- Dependencies and connections are attached **before** the sandbox is closed, so
  they still work.
- The `excel` extension is preloaded, so `read_xlsx(source('…'))` works. Other
  extensions are loaded only by a bound connection's driver.
- Reading a URL (`https://…`), another directory, `INSTALL`/`LOAD`, or turning
  external access back on fails with a `Permission Error`.

If a build that worked before starts failing with a `Permission Error` (for example,
a connection driver that turns out to need external access), an admin can switch
off `build.sandbox`. Report queries are always sandboxed.

## Limits

- **Memory.** A build runs with the `build.memory_limit` setting (default 4GB, or
  DuckDB's own default when `build.memory_duckdb_default` is ticked) and
  spills to disk beyond it.
- **Time.** A build still running after `build.timeout_seconds` (default one hour)
  is interrupted and fails; the previous version stays live.
