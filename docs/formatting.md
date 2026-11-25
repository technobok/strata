# Formatting reports

A report is plain data: its SQL decides the rows. How those rows are shown is a
separate set of settings per report: number and date formats, alignment, hidden
columns, links and styles. They never change the data, and they never come from the SQL.

A report's owners set them on its **Format page** (the **Format** button on the
report and on its edit page); they are also part of the report's
[definition file](import-export.md#formatting) (`[[report.column]]`).

## The Format page

The page lists the report's columns as they come out of its SQL on the datamodule's
live data, so the datamodule must have been built. For each column:

- **Format**: an Excel format code. The box offers codes that suit the column (the
  common ones, then those other reports use), and you can type any other. Beside it,
  the column's first value with that format.
- **Align**, **Hide**: see [Alignment](#alignment) and [Hidden columns](#hidden-columns).
- **Link**: a link template, with **Insert a column…** to add `{Column}` where the
  cursor is; the link text (blank = the cell's value); **New tab**. Beside it, the
  link the first row gets.

Below the columns, **Styles** holds the [style rules](#styles): **Add a rule**, then
its condition, the columns it styles (none = the whole row) and its words.

Each change is checked as you type: a field with a problem turns red with the reason
beside it (e.g. a date format on a number column, or a column name that does not
exist, with "did you mean …?"). **Save** refuses while anything is wrong and lists
every problem. The **Preview** shows the report's first page with the settings as
they stand, before you save; a required argument without a default asks for a value
first.

Settings of a column the report no longer has (its SQL changed, or a rebuild dropped
it) are listed under **No longer in the result**: they are never dropped quietly.
Tick **Remove** to drop them, or bring the column back.

## Without settings

Every value shows exactly as it is stored, so nothing is hidden from you:

- integers and decimals in full, with no rounding and no thousands separators; a
  decimal keeps all its places, trailing zeros included (a `DECIMAL(38,20)` from
  Navision shows 20 places until you give it a format);
- floating-point numbers in their shortest exact form (`0.1`);
- dates `2026-11-25`, times `14:30:00`, timestamps `2026-11-25 14:30:00` (with the
  fraction of a second when there is one, and the offset for a time zone);
- booleans `true` / `false`; NULL as an empty cell.

Numbers, dates and times line up on the right, text on the left.

## Formats

A column's `format` is an **Excel number format code**: the same code works in
Strata and in Excel, and an xlsx download carries it as the cell's number format.
Strata reads a part of Excel's language; anything else is refused when the settings
are saved, saying what and where (it is never quietly ignored).

| Code | 1234.5 | -1234.5 | 0.256 |
|---|---|---|---|
| `0` | 1235 | -1235 | 0 |
| `0.00` | 1234.50 | -1234.50 | 0.26 |
| `#,##0` | 1,235 | -1,235 | 0 |
| `#,##0.00` | 1,234.50 | -1,234.50 | 0.26 |
| `#,##0.00;(#,##0.00)` | 1,234.50 | (1,234.50) | 0.26 |
| `0.0%` | 123450.0% | -123450.0% | 25.6% |
| `#,##0,"k"` | 1k | -1k | 0k |
| `"$"#,##0.00` | $1,234.50 | -$1,234.50 | $0.26 |

### Numbers

- `0` is a digit that always shows; `#` a digit that shows only when needed; `?` a
  digit or a space as wide as one, so decimals line up.
- `.` is the decimal point. Values round half away from zero (2.5 → 3, -2.5 → -3),
  as Excel does.
- `,` between digits adds thousands separators; each `,` at the end divides by 1,000
  (`0.0,,"M"` shows millions).
- `%` multiplies by 100 and shows `%`.
- Text: in double quotes (`" kg"`), or one character after a backslash (`\h`). These
  show as themselves without quotes: `$ - + / ( ) : ! ^ & ' ~ { } < > =` and space.
  `_)` leaves a space (as wide as the character after `_`), `*-` is ignored (Excel
  uses it to fill a cell), and `[$€-407]` shows `€`.
- Sections, separated by `;`: `positive;negative;zero;text`. With two or more, the
  negative section shows the number without its minus sign, so you write the sign
  or brackets yourself (`0.00;(0.00)`). An empty section shows nothing (`0;;` hides
  negatives and zeros). With one section, a negative number gets a `-`.
- `General` shows the value plain, as without a format.
- A colour at the start of a section: `[Red]`, `[Blue]`, `[Green]`, `[Yellow]`,
  `[Cyan]`, `[Magenta]`, `[Black]`, `[White]`. `#,##0.00;[Red]-#,##0.00` shows negative
  amounts in red. Strata shows them as the [style colours](#styles) of the same names
  (Magenta is `fuchsia`; Black and White are the page's own text and background), so
  they read in dark mode too; an xlsx download keeps Excel's own. A style rule's colour
  wins over a format's.

### Dates and times

| Code | Shows |
|---|---|
| `yyyy` / `yy` | 2026 / 26 |
| `m` / `mm` / `mmm` / `mmmm` / `mmmmm` | 11 / 11 / Nov / November / N |
| `d` / `dd` / `ddd` / `dddd` | 5 / 05 / Thu / Thursday |
| `h` / `hh` | 9 / 09 (0–23, or 1–12 with AM/PM) |
| `m` / `mm` right after an hour or right before seconds | minutes |
| `s` / `ss`, then `.0` `.00` `.000` | seconds and their fraction |
| `AM/PM` `am/pm` `A/P` `a/p` | a 12-hour clock |

So `d mmm yyyy` shows `5 Nov 2026`, `yyyy-mm-dd hh:mm` shows `2026-11-05 14:03`, and
`h:mm AM/PM` shows `2:03 PM`. When seconds are shown they are rounded to the places
shown; a code without seconds cuts them off (14:30:59 shows `14:30` with `hh:mm`).
Month and day names are English. A TIME column takes only `h`, `m` and `s`.

### Text

A text column takes `@`, the text itself, with words around it: `"Item "@`.

### Not supported

Scientific notation (`0.00E+00`), fractions (`# ?/?`), conditions (`[>100]`),
elapsed time (`[h]:mm`) and numbered colours (`[Color10]`) are refused for now.
For a colour that depends on the value, use a [style rule](#styles).

A format must suit its column: number codes on numbers, date codes on dates, times
and timestamps, `@` on text. A format that does not fit is refused when saved.

## Alignment

`align` is `auto` (numbers, dates and times right; text left), `left`, `center` or
`right`. The column's heading follows it. An xlsx download carries it too.

## Links

A column's `link` turns each of its cells into a link, made from the row the cell is
in. Links appear on the report page and the run page only: never in mail or
downloads.

- `{Column}` is that column's value in the same row, encoded so that spaces, `&` and
  `/` cannot break the link. A column name may have spaces: `{Customer Code}`.
- `{+Column}` is the value as it is, for a column that already holds a path or a
  whole address.
- `{{` and `}}` are a literal `{` and `}`.
- When a value in the link is empty (NULL), that cell shows plain text, no link.

Where it goes depends on how the link starts:

| Starts with | Goes to | Example |
|---|---|---|
| a scheme | that address | `https://shop.example/items?no={Item}` |
| `/` | the same site as Strata | `/nav?co={Company}&item={Item}` |
| anything else | a page of Strata | `r/<report id>.html?customer={Customer}` |

The third is a drill-down: a link from one report to another, filtered to this
row (see [Report URLs](urls.md#filters) for what a report URL can carry).

Links may use `http`, `https` and `mailto`, and any scheme an admin adds to the
setting [`links.extra_schemes`](settings.md#reports--apply-immediately). The address
is checked again as each cell is shown, because a `{+Column}` value could carry
anything: a link that would go elsewhere (e.g. `javascript:`) shows as plain text.

The link's text is the cell's (formatted) value; `link_text` replaces it with a
template of its own, e.g. `Open {Item}`. `new_tab = true` opens the link in a new
tab.

## Styles

A style rule colours or emphasises the rows its condition is true for: the whole row,
or only the columns it names.

| When | Apply to | Style |
|---|---|---|
| `"Status" = 'Blocked'` | the whole row | `grey italic` |
| `"Amount" < 0` | Amount | `red bold` |
| `"Due" < current_date AND NOT "Paid"` | Due | `bg-amber` |

**When** is a condition in SQL (DuckDB), written as in a `WHERE` clause over the
report's columns: put a column name in double quotes (`"Amount"`), text in single
quotes (`'Blocked'`). A row where it is NULL counts as false. **Insert a column…**
adds a quoted name. It is checked against the report's data when saved.

**Style** is one or more words:

- `bold`, `italic`, `underline`, `strike`;
- a colour for the text: `red` `pink` `fuchsia` `purple` `violet` `indigo` `blue`
  `azure` `cyan` `jade` `green` `lime` `yellow` `amber` `pumpkin` `orange` `sand`
  `grey` `zinc` `slate`. Each is a shade Strata picks for the light theme and another
  for the dark one, both easy to read on the page's background;
- `bg-` and a colour for the background: `bg-amber`, also theme-aware;
- an exact shade, the same in both themes: `red-550`, `bg-red-100` (shades 50 to 950,
  in steps of 50; check it reads in both themes below).

A cell takes every rule that is true for its row, in order: words add up, and a later
rule's colour or background replaces an earlier one's. Styles show on the page and the
run page, and in mail (as the light theme).

When a rule no longer runs on the data (a column it names was dropped), the page shows
without styles and tells the owners why; the Format page marks the rule.

<div data-palette></div>

## Hidden columns

`hidden = true` hides a column on the page and in mail. It is still there for link
templates and for filters in the URL, and data downloads (CSV, JSON, Parquet, xlsx)
keep it. The search box does not look in hidden columns, on the page or in a
download, so a row never matches because of something nobody can see.

## Where each setting applies

| | page and run page | mail | xlsx download | CSV, JSON, Parquet |
|---|---|---|---|---|
| formats, alignment | yes | yes | the cell's number format | raw values |
| styles | yes | yes | no | no |
| links | yes | no | no | no |
| hidden | hidden | hidden | kept | kept |

A saved run is shown with the report's settings as they are now: a run keeps its
rows, not its look. Settings for a column the run does not have are skipped.

When a setting no longer fits a report (after a change to its SQL, or a rebuild that
dropped a column), that part is skipped and the cell shows plain: a report never
fails to show because of its formatting.
