# Report URLs

Every report is served at:

```
/r/<report id>.<format>?<arguments>&<filters>&<sort, search, page>
```

`<format>` is `html`, `parquet`, `csv`, `json` or `xlsx`. The HTML page needs a login;
the data formats also accept an API key (see [API keys and Excel](api-keys.md)).

Each part of the query string is described below. Everything a reader does on the
HTML page (sort, search, filter, page) ends up in the URL, so a copied link shows
the same view, and swapping `.html` for `.csv` downloads the same rows.

## Arguments

A report's declared arguments go by name: `?region=North&from_date=2026-01-01`. Repeat
a multiselect argument for each value: `?regions=North&regions=South`. See
[Reports](reports.md#arguments).

## Filters

A filter narrows the report's output by one of its **output columns**:
`column__operator=value`. Several filters must all match.

| Filter | Keeps rows where | Example |
|---|---|---|
| `col__eq=v` | the column equals `v` | `region__eq=North` |
| `col__ne=v` | the column is not `v` | `status__ne=closed` |
| `col__gt=v` | the column is greater than `v` | `revenue__gt=1000` |
| `col__gte=v` | the column is `v` or greater | `sold_on__gte=2026-01-01` |
| `col__lt=v` | the column is less than `v` | `qty__lt=0` |
| `col__lte=v` | the column is `v` or less | `sold_on__lte=2026-03-31` |
| `col__in=a,b,c` | the column is one of the comma-separated values | `region__in=North,South` |
| `col__between=a,b` | the column is from `a` to `b`, inclusive | `revenue__between=100,500` |
| `col__contains=text` | the column contains `text`, ignoring case | `product__contains=widget` |
| `col__isnull=true` | the column is empty (`false`: is not empty) | `email__isnull=true` |

A bare `column=value` (no operator) is an `eq` filter, unless the name is one of
the report's arguments; an argument always wins.

Column names are matched exactly. In a hand-typed URL, `_` also matches a space,
so `net_sales__gt=0` finds the column `net sales` when there is no `net_sales`.
Values are always passed to DuckDB as parameters and converted to the column's
type, so a URL can never change the SQL.

## Search, sort and page

| Key | Meaning | Example |
|---|---|---|
| `q` | Rows where **any** shown column contains the text, ignoring case: the stored value, not its format, and not a [hidden column](formatting.md#hidden-columns). Repeat `q` to require several fragments. | `q=widget&q=north` |
| `sort` | Columns to sort by, comma-separated; `-` for descending. Empty values sort last. | `sort=-revenue,region` |
| `page` | 1-based page of 200 rows. The HTML page is always paged; the data formats return every row unless `page` is given. | `page=3` |
| `show` | On the HTML page of a report with [subtotals](formatting.md#totals-and-subtotals): a group column, to show only the totals at that group's level; `rows` (the default) shows the rows. Data formats always return rows. | `show=Company` |

## Version pinning

`version=<version id>` serves an older retained version of the datamodule instead of
the live one. It never triggers a build. The datamodule page links each version to
each report; the HTML page shows a banner with a link back to live data.

## Reserved names

These keys are never arguments or filters, and a report cannot have an argument
with these names:

| Key | Meaning |
|---|---|
| `sort` | Sort order (above). |
| `page` | Page number (above). |
| `q` | Search (above). |
| `version` | Version pinning (above). |
| `show` | Totals view (above). |
| `key` | An API key token (see [API keys and Excel](api-keys.md)). |
| `format` | Accepted and ignored: the extension in the path sets the format. |

Any other key that is neither an argument nor a column is an error: HTTP 400 for
the data formats, an inline message on the HTML page.

## Responses and headers

| Situation | Data formats | HTML |
|---|---|---|
| Fresh data | 200 with `X-Data-As-Of: <build time>` | 200, "Data as of …" |
| Stale data (rebuild slow or failed) | 200 with `X-Data-As-Of` and `X-Data-Stale: 1` | 200 with a stale banner |
| Never built yet | 503 with `Retry-After: 30` | a page that reloads itself |
| Bad argument, filter or key | 400 with the reason | the page with the reason |
| Not logged in, no API key | 401 | redirect to login |
| Invalid, disabled, expired or out-of-scope API key | 403 | (keys never open HTML) |

"Stale" is explained in [Freshness and builds](freshness.md#stale-data).
