# API keys and Excel

Excel and PowerQuery cannot log in through single sign-on. An **API key** lets them
fetch a report's **data** (Parquet, CSV, JSON or XLSX) without a login. A key never
opens the HTML page.

## Creating a key

On the datamodule page, under **API keys**, choose:

| Field | Meaning |
|---|---|
| Name | What the key is for, e.g. `finance workbook`. |
| Scope | The whole datamodule (every report on it) or one report. |
| Expires | Optional; the key works through that day (UTC). |
| Fixed args | Argument values the key always sends, one `name=value` per line. They win over anything in the URL. Repeat a name for a multiselect. |
| Caller-suppliable args | The argument names a URL may set, comma-separated. Blank means every argument that is not fixed. |

After **Create**, the token and a ready-made URL for each report are shown **once**,
with copy buttons. Strata stores only a hash of the token, so it cannot show the token
again. If it is lost, **rotate** the key.

Fixed args are how one report serves several audiences safely. For example, a key
with `company=BRS` fixed always gets BRS rows, whatever the URL says.

## Using it in Excel

1. Copy the report's URL from the key's reveal box. It looks like
   `https://<server>/strata/r/<report id>.parquet?key=<token>`.
2. In Excel: **Data → Get Data → From Other Sources → From Web**, and paste the URL.
3. Load the table. **Refresh** fetches the current data again.

Parquet is the default because it keeps column types (dates stay dates). Swap the
extension for `.csv`, `.json` or `.xlsx` if needed.

Arguments, filters, sort and search all work with a key. Add them to the URL as on
any report (see [Report URLs](urls.md)):

```
…/r/<report id>.parquet?key=<token>&from_date=2026-01-01&region__in=North,South
```

Excel waits about 100 seconds for a response. When a report's data is stale, Strata
waits at most `serve.max_wait_seconds` (default 60) for a rebuild before serving the
previous data, so a refresh does not time out (see
[Freshness and builds](freshness.md#stale-data)).

## Managing keys

- **Rotate** issues a new token and stops the old one at once. Workbooks using the
  old URL need the new one.
- **Disable** stops a key without deleting it; **enable** turns it back on.
- **Delete** removes it.

Every request made with a key is logged: format, rows, bytes, time taken, status and
the caller's address. The key's use count and last-used time are on the datamodule
page.

A wrong, disabled, expired or out-of-scope token gets HTTP 403. A data request with
no login and no key gets 401.

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
