# Read audit

Strata records every read of a datamodule's data: who or which key, what, when,
what came back and from where. Refused requests are recorded too. Use it to answer
"who has read payroll this month?" or "is anyone still using this Excel link?".

## Where to see it

- **Admins:** **Admin** → **Reads (audit)** lists every read of every
  datamodule.
- **A datamodule's owners:** the **Reads** button on its page lists its reads only.
- **One key's reads:** the **reads** link under a key's use count, on the datamodule
  page and on Admin → API keys.

Filter by datamodule, report, person, kind and dates (in UTC), or show refused
requests only. **Download CSV** saves every row the filter matches, with all
columns, for an auditor.

## What is recorded

One row per request. These are the kinds:

| Kind | The request |
|---|---|
| `page` | A report page, and each sort, search or page change on it. |
| `data` | A report's data: an export link, or Excel refreshing with a key. |
| `run` | A saved run's page, and each sort, search or page change on it. |
| `run-data` | A saved run's download. |
| `excel-key` | A report's **Use in Excel** box was opened, or its key replaced. This shows the key to that person. |
| `preview` | **Preview rendered script** on a datamodule page. It reads the dependencies' rows. |
| `script` | A version's build script. A rendered Jinja script can hold the rows it looped over. |
| `source` | An uploaded source file was downloaded. |

Each row holds:

- **who**: the person signed in; or the key, with its name and the first
  characters of its token; or, for a token that matches no key, those characters
  alone;
- **what**: the datamodule, report, run or version, and the arguments, filters,
  sort and search from the link (never the key itself);
- **the outcome**: the status, the rows sent or shown, the bytes and the time taken;
- **from where**: the client's address and its browser or program (Excel shows as
  `Microsoft.Data.Mashup`).

A row keeps the names of what it read, so the history stays readable after a key,
report or datamodule is deleted.

## Refused requests

A refused request is recorded once Strata knows which report, run or file was asked
for. That includes someone who is not a viewer, a locked datamodule, a link with no
key, a wrong, disabled or expired key, bad arguments, and data that has not been
built yet. Refused rows are tinted on the page; **refused only** shows just them.

## What is not recorded

- **Mail.** Each run records who it was mailed to. A restricted datamodule's mail
  carries no rows, only a link, and opening that link is recorded.
- **The SQL console.** It reads Strata's own settings database, not datamodules.
- **Builds.** A build reads its sources, connections and dependencies; the
  datamodule page lists its builds.
- **The datamodule page.** Its owners see every key's links there; only the **Use
  in Excel** box is recorded as `excel-key`.

## How long rows are kept

The `audit.retention_days` setting, 365 days by default, sets how long rows are
kept. `0` keeps them all. The worker deletes older rows as it runs, and so does
`strata-admin reconcile`. See [Settings](settings.md#read-audit--apply-immediately).
