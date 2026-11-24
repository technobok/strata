# Reports

A **report** is one SELECT over its datamodule's tables, with optional **arguments**
the reader fills in. Strata applies it to the live version each time the report is
read, so editing a report never needs a rebuild.

## The report body

Write the body as a single SELECT. Refer to an argument as `$name`:

```sql
SELECT region, product, revenue, sold_on
FROM sales
WHERE region = $region
  AND sold_on >= $from_date
ORDER BY sold_on
```

The body must be **exactly one SELECT statement**. `WITH …`, `FROM …`, `VALUES` and
`UNION` all count as one. Anything else (a second statement, `CREATE`, `COPY`,
`ATTACH`, …) is refused when you save the report, and again when it is read.

A bare `PIVOT` parses as a statement of its own, so wrap it:

```sql
SELECT * FROM (PIVOT sales ON region USING sum(revenue) GROUP BY product)
```

## Names

A report's **name** is its title everywhere: lists, its page, mail subjects. No two
reports on a datamodule may have the same name, in any mix of capitals. Its
optional **description** says what it shows, or for a check what to do about the
rows it finds. It is shown under the name on the report's page and at the top of
its mail.

Strata also gives each report a **macro name**, made from its name when the report
is created: `Bad item department` becomes `bad_item_department`. Strata runs the
report under that name, and downloads are named after it. In an `.xlsx` it names the
sheet, and the Excel table on that sheet is `<macro name>_table`. Power Query's
Navigator lists both; choose the table. The macro name never changes, even when the
report is renamed, because a workbook's query picks the sheet or table by name. The
report's edit page shows both names.

## Saving checks it runs

When the datamodule has data, saving a report first checks that it **runs on that
data**: DuckDB works out the report's columns without reading any rows, so a table
or column that does not exist, a type error, an argument default that is not of its
type, or a broken constraint query is found at once. Such a report is **not saved**:
the form shows DuckDB's error and keeps everything you typed. The check uses each
argument's default (a required argument without one counts as a value of its type).

A datamodule that has never been built has nothing to check against, so its reports
save as long as they are one SELECT. Build it first to have them checked.

A build can break a report that ran, e.g. when the script no longer creates a table
the report reads. After every successful build, Strata checks each report again: a
report that no longer runs is marked **does not run on this data** in the
datamodule's Reports table, its edit page shows the error, and the datamodule's
alert list is mailed (see [Freshness](freshness.md#alerts)). The mark goes once the
report, or the build, is fixed.

## Arguments

Each argument has:

| Field | Meaning |
|---|---|
| Name | How the body refers to it (`$name`) and its key in the URL (`?name=value`). |
| Type | `text`, `integer`, `number`, `decimal`, `date`, `time` or `boolean`. The value is converted to this type before the query runs. |
| Widget | `input` (a box to type in), `select` (pick one) or `multiselect` (pick several). |
| Required | A required argument with no value and no default is an error. |
| Default | Used when the URL gives no value. For a multiselect, separate the values with commas. |
| Constraint | A query that lists the choices for a `select` or `multiselect`. |
| Description | Shown next to the field. |

An optional argument with no value and no default is `NULL`. Write the body so that
`NULL` means "all":

```sql
SELECT * FROM sales
WHERE ($region IS NULL OR region = $region)
```

A **multiselect** passes a list. Test membership with `list_contains`:

```sql
SELECT * FROM sales WHERE list_contains($regions, region)
```

In a URL, repeat the name for each value: `?regions=North&regions=South`.

These names are reserved and cannot be argument names: `sort`, `page`, `q`,
`format`, `key`, `version` (see [Report URLs](urls.md#reserved-names)).

## Constraint queries

A constraint query fills a select or multiselect with the choices found in the
live data. Like the body, it must be one SELECT:

```sql
SELECT DISTINCT region FROM sales ORDER BY region
```

With one column, each value is both what is sent and what is shown. With two
columns, the first is the value and the second is the label:

```sql
SELECT code, name FROM companies ORDER BY name
```

The choices are worked out once per live version. A query that fails leaves the
list empty and is logged.

## Reading a report

- Opening a report runs it at once with its arguments' defaults. Only a **required**
  argument with **no default** makes the page wait for you to fill it in and press
  **Run**. Change any argument and press **Run** again to see other rows.
- A small table at the top says how old the data is (**Data as of**, when its
  datamodule was last built), which argument values are in use (and which are
  defaults), and how many rows came back. A **saved run** (Run now, or a schedule)
  has the same table, with its values fixed: open **View current** to change them.
- The table has sortable headers, a search box, filter chips and a pager. Every
  change is kept in the URL, so a link reproduces the same view.
- The export links download the same rows as Parquet, CSV, JSON or XLSX.

See [Report URLs](urls.md) for everything a URL can carry, and
[API keys and Excel](api-keys.md) for refreshing a report from Excel.
