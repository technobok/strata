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
and the build can then read from it:

- **SQLite and PostgreSQL** are attached as a database named after the local name,
  so tables are `name.schema.table`:

  ```sql
  CREATE TABLE orders AS SELECT * FROM warehouse.public.orders;
  ```

- **ODBC** (SQL Server through FreeTDS) gives the script a connection handle in the
  variable `conn_<name>`. Pass the remote query as text; it runs on the server, and
  only its result comes back:

  ```sql
  CREATE TABLE orders AS
  SELECT * FROM odbc_query(getvariable('conn_navi'),
                           'SELECT OrderID, CustID, Total FROM dbo.Orders');
  ```

Credentials are stored encrypted. The build script never sees them.

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

- **Memory.** A build runs with the `build.memory_limit` setting (default 4GB) and
  spills to disk beyond it.
- **Time.** A build still running after `build.timeout_seconds` (default one hour)
  is interrupted and fails; the previous version stays live.
