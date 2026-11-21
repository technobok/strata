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

The **macro name** is the SQL name Strata gives the report internally. It must be a
plain identifier (letters, digits, `_`), unique within the datamodule.

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

- **Run** on the report page opens it in the browser.
- The table has sortable headers, a search box, filter chips and a pager. Every
  change is kept in the URL, so a link reproduces the same view.
- The export links download the same rows as Parquet, CSV, JSON or XLSX.

See [Report URLs](urls.md) for everything a URL can carry, and
[API keys and Excel](api-keys.md) for refreshing a report from Excel.
