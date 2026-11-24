# API keys and Excel

Excel and PowerQuery cannot log in through single sign-on. An **API key** lets them
fetch a report's **data** (Parquet, CSV, JSON or XLSX) without a login. A key never
opens the HTML page. There are two kinds:

- each datamodule's **Excel link**: one key for all its reports, which anyone who
  can view them copies from a report page's **Use in Excel** box;
- **named keys**, made on a datamodule page, with fixed arguments if needed: for
  another program, or a workbook that must always get the same rows.

A report's data URL names the report in its path and carries the key in its query:
`…/strata/r/<report id>.xlsx?key=<token>`. A key for a datamodule works for every
report on it: only the report id changes. A workbook can send the key in a request
header instead, so the link has no key (see
[Keeping the key out of the link](#keeping-the-key-out-of-the-link)).

## Excel links

On a report page, open **Use in Excel** (next to the export links). It shows a link
for exactly the view you are looking at: its arguments, filters, sort and search.
Pick a format, **Copy**, and follow [Using a key in Excel](#using-a-key-in-excel).

- The link carries the **datamodule's Excel key**: one per datamodule, made with the
  datamodule, and the same for everyone. It reads every report on that datamodule,
  and nothing else.
- It belongs to the datamodule, not to a person: anyone with a workbook built from it
  can refresh it, and it keeps working when the person who made the workbook leaves.
- A workbook refreshes from wherever Excel reaches the server directly. Behind a
  sign-in Excel cannot pass, such as Microsoft's in front of the server from outside
  the office, it does not refresh.
- **Replace the datamodule's Excel key** (in the box, or **Replace** on the
  **Excel link key** line under the datamodule page's Reports table; for its owners
  and the admins) cuts off every workbook on any of its reports at once: each needs a
  new link. That is the answer to a workbook that reached the wrong people, or to
  someone leaving the datamodule's viewers (see [Access](access.md#excel-links-and-keys)).
- **Disable** on the same line stops every workbook until it is enabled again, with
  the same links. The Excel key cannot be deleted: every datamodule has one.
- Until 2026-11-23 each report had its own Excel key. Those links keep working until
  the datamodule's key is first replaced, which disables them too.
- A **locked** datamodule stops every key on it (see [Access](access.md#locks)).
- Admins see every key, with its last use, under **Admin → API keys**.

## Named keys

On the datamodule page, under **Named keys** (closed until you open it: most
datamodules need none), its owners make extra keys for another program, or for a
workbook that needs fixed arguments, an expiry date, or a key that can be cut off
without affecting anyone else. A key reads **every report of the datamodule**. Choose:

| Field | Meaning |
|---|---|
| Name | What the key is for, e.g. `finance workbook`. |
| Expires | Optional; the key works through that day (UTC). |
| Fixed args | Argument values the key always sends, one `name=value` per line. They win over anything in the URL. Repeat a name for a multiselect. |
| Caller-suppliable args | The argument names a URL may set, comma-separated. Blank means every argument that is not fixed. |

Each key's links (one per report, `.xlsx`) stay in the Named keys table, under its
token prefix: one **Copy: <report>** button per report (hover to see the link). Its
owners can copy them again at any time. Strata
looks a key up by a hash of its token and keeps the token itself encrypted, like the
Excel link. Keys made before 2026-11-23 kept only the hash: **rotate** one once to see
its links. Keys made before then for one report keep reading only that report.

Fixed args are how one report serves several audiences safely. For example, a key
with `company=BRS` fixed always gets BRS rows, whatever the URL says.

## Using a key in Excel

1. Copy the report's URL with the key: from the report's **Use in Excel** box (for the
   view you are looking at), from the **Excel** column of the datamodule page's Reports
   table, or from a named key's links. It looks like
   `https://<server>/strata/r/<report id>.xlsx?key=<token>`.
2. In Excel: **Data → Get Data → From Other Sources → From Web**, and paste the URL.
3. When Excel asks how to connect, choose **Anonymous** (the key is the credential).
   Excel remembers that choice per person, so a colleague refreshing your workbook for
   the first time may be asked too.
4. Load the table. **Refresh** fetches the current data again.

Use **`.xlsx`** (the box's default): From Web opens it as a table, with dates and
numbers already typed. `.csv` and `.json` open too, but Excel guesses the column types.
Excel cannot use a **`.parquet`** link: its Parquet support reads only local files and
Azure storage, so the link loads as an unopened file. Parquet is for Power BI and
other tools (Python, DuckDB).

Arguments, filters, sort and search all work with a key. Add them to the URL as on
any report (see [Report URLs](urls.md)):

```
…/r/<report id>.xlsx?key=<token>&from_date=2026-01-01&region__in=North,South
```

Excel waits about 100 seconds for a response. When a report's data is stale, Strata
waits at most `serve.max_wait_seconds` (default 60) for a rebuild before serving the
previous data, so a refresh does not time out (see
[Freshness and builds](freshness.md#stale-data)).

## Keeping the key out of the link

A workbook can send its key in a request **header** instead of in the link. The link
then has no `key=`, so a copied link, Excel's **Data Source Settings** list and any
request log never show the key. The workbook still holds the key, in its query: anyone
with the file can refresh it and can read the key, so keep such workbooks in protected
folders either way. **Replace** still cuts every workbook off.

| Header name | Value |
|---|---|
| `Authorization` | `Bearer <token>`: the word `Bearer`, one space, then the key |

`<token>` is the end of the link, after `key=`. Strata's links always put the key
last: in `…/r/<report id>.xlsx?region=North&key=AbC123`, the token is `AbC123`, and
the link without its key is `…/r/<report id>.xlsx?region=North` (drop the `&` or `?`
before `key=` too).

Power Query sends an `Authorization` header only when it connects **Anonymously**,
which is what Strata's links use.

### A new workbook

1. Copy the report's link (as in [Using a key in Excel](#using-a-key-in-excel)) and
   split it into the link without its key, and the token.
2. In Excel: **Data → Get Data → From Other Sources → From Web**, and choose
   **Advanced** at the top of the dialog.
3. **URL parts**: paste the link without its key.
4. **HTTP request header parameters**: type `Authorization` in the left box and
   `Bearer <token>` in the right one.
5. **OK**, choose **Anonymous**, then **Connect**.

### Moving a workbook from the link to the header

1. In Excel: **Data → Queries & Connections**, right-click the query, **Edit**.
2. In Power Query: **Home → Advanced Editor**. The `Source` line holds the link,
   for example:

   ```
   Source = Excel.Workbook(Web.Contents("https://<server>/strata/r/<report id>.xlsx?region=North&key=<token>"), null, true),
   ```

3. Take `key=<token>` (and the `&` or `?` before it) out of the link, and add the
   header after the link, inside `Web.Contents( … )`:

   ```
   Source = Excel.Workbook(Web.Contents("https://<server>/strata/r/<report id>.xlsx?region=North", [Headers=[Authorization="Bearer <token>"]]), null, true),
   ```

   Leave the rest of the line as it is: `Excel.Workbook(…, null, true)` for `.xlsx`,
   `Csv.Document(…)` for `.csv`, `Json.Document(…)` for `.json`.
4. **Done**, then **Home → Refresh Preview**. If Excel says the `Authorization`
   header is only supported when connecting anonymously, set the server to Anonymous:
   **Data → Get Data → Data Source Settings**, select the server, **Edit
   Permissions**, **Anonymous**.
5. **Close & Load**. The data loads with no key in the link, so the key came in the
   header. The [read audit](audit.md) lists the request under the key's name.

When the key is replaced, put the new token after `Bearer ` in the header.

What a refusal means:

- **400** "two different keys": the link still has a `key=` and it is not the key in
  the header. Take the key out of the link. The same key in both works.
- **401** "authentication required": no key arrived. Check the header name is
  `Authorization` and the value starts with `Bearer `.
- **403** "invalid API key": the key is wrong, replaced, disabled or expired.

## Managing named keys

- **Rotate** issues a new token and stops the old one at once. Workbooks using the
  old URL need the new one.
- **Disable** stops a key without deleting it; **enable** turns it back on.
- **Delete** removes it.

Every request made with a key, the Excel key or a named one, is in the [read audit](audit.md):
the format, the arguments, rows, bytes, time taken, status, the caller's address and
program, and refusals too (a wrong, disabled or expired key). The key's use count and
last-used time are on the datamodule page; the **reads** link under them lists that
key's requests.

A wrong, disabled or expired token, or one for another datamodule, gets HTTP 403. A data request with
no login and no key gets 401. Two different keys, one in the link and one in the
header, get 400.

## Testing Excel's sign-in (the auth probe)

The address in front of Strata signs people in with Entra (Microsoft's work
accounts) before any request reaches Strata. The auth probe shows what an Excel
request still carries when it arrives, so a test can tell whether Excel's
**Organizational account** sign-in can work, or whether workbooks need a key.

Turn on `diag.auth_probe` (see [Settings](settings.md#diagnostics--apply-immediately)).
While it is on, `/diag/auth-probe.json` answers anyone who reaches it, with no
login, and writes the same answer to the app log (`docker logs strata-app`). It is
`404` while the setting is off.

It shows the browser or program (User-Agent), the names of the headers and cookies
that arrived, who Strata's own sign-in sees (`gatekeeper_user`), and the shape of
any `Authorization` header: the scheme, and for a sign-in token (a JWT) who it is
for (`aud`), from which tenant (`iss`, `tid`), for whom (`upn`) and through which
program (`appid`). It never shows a token, a password or a cookie's value.

Excel's Organizational account first asks the address what sign-in it needs; it
signs in only if the answer is a `401` naming the Entra tenant. Add
`?challenge=<tenant>` to get that answer when no token came:

```
…/strata/diag/auth-probe.json?challenge=<tenant>
```

`<tenant>` is the organisation's Entra tenant: the middle of Gatekeeper's issuer
(**Gatekeeper → Admin → SSO**, `https://login.microsoftonline.com/<tenant>/v2.0`),
or the company's email domain. It is a public name, not a permission. Letters,
digits, `.` and `-` only; anything else gets `400`.

The test:

1. In **Admin → Configuration**, tick `diag.auth_probe` and save.
2. In a browser, open `https://<server>/strata/diag/auth-probe.json`. The answer
   should show your name in `gatekeeper_user`.
3. In Excel: **Data → Get Data → From Other Sources → From Web**, paste the same URL,
   choose **Organizational account**, **Sign in**, **Connect**.
4. Clear the saved sign-in (**Data → Get Data → Data Source Settings**, select the
   server, **Clear Permissions**), then repeat step 3 with `?challenge=<tenant>`.
5. Repeat step 3 with **Anonymous**.
6. Note what each attempt gave: data, a message (with any `AADSTS…` code) or a login
   page; the answer Excel showed; and the Excel version (**File → Account → About
   Excel**). The app log has every request that reached Strata:
   `docker logs strata-app 2>&1 | grep "auth probe"`.
7. Untick `diag.auth_probe`.

To test whether Excel can send a key in a request header instead of in the link:
in step 3 choose **Advanced**, add the header `Authorization` with the value
`Bearer test-123`, and choose **Anonymous**. The answer's `authorization` should
show `scheme` `Bearer` and `length` 8, and `headers` should list `Authorization`.
From the office this worked on 2026-11-24 (see
[Keeping the key out of the link](#keeping-the-key-out-of-the-link)). In Power Query
a nested value shows as **Record** or **List**: click beside it to preview it, or
change `Json.Document(` to `Text.FromBinary(` in the `Source` step to see the whole
answer as text.
