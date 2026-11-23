# Freshness and builds

Strata never makes a reader wait for a long build. Builds run in the background in
`strata-worker`. A report serves the newest data it can get within a short wait, and
otherwise the previous version, marked as stale.

## TTL

A datamodule's **TTL** (time to live, in seconds) is how old its live version may
get before it counts as stale:

| TTL | Meaning |
|---|---|
| blank | Never goes stale on its own. Rebuild it with the Build button or a schedule. |
| `0` | Always stale: every read asks for a rebuild. |
| `3600` | Stale one hour after the build finished. |

A schedule that rebuilds just before the TTL runs out keeps reads fresh without
anyone waiting (see [Schedules](schedules.md)).

## Stale data

When a report is read and its data is stale:

1. Strata asks the worker for a rebuild. Requests made at the same time share one
   build.
2. The request waits up to `serve.max_wait_seconds` (default 60) for it. Excel gives
   up at about 100 seconds, so the default leaves room.
3. If the rebuild finishes in time, the new data is served.
4. If it does not finish in time, or fails, the **previous version** is served,
   marked as stale. The HTML page shows "Showing data as of …" in a banner; the data
   formats send `X-Data-Stale: 1`.

Every response says how old its data is: "Data as of …" on the HTML page,
`X-Data-As-Of` for the data formats. A datamodule that has never been built returns
HTTP 503 with `Retry-After` (the HTML page reloads itself).

## Builds and the worker

A build is queued by:

- the **Build** button (or **Build with all dependencies**);
- a **schedule** when it is due;
- a report read that finds the data stale.

The worker runs it. Only one build of a datamodule runs at a time, even across
several worker processes. A Build click made during a build queues exactly one
follow-up, so an edit made mid-build is still built.

- **Worker queue.** Each datamodule names a queue (default `default`). A worker
  takes jobs from the queues it is started with. A second worker on a `long` queue
  keeps slow extracts from delaying quick ones.
- **Health.** The datamodule page lists recent build jobs and warns when no worker
  has checked in for two minutes. The admin page lists running workers.
- **A worker that dies mid-build.** Its build is marked failed within about a minute,
  and the next build takes over.

## Failed builds

A failed build changes nothing that readers see: the previous version stays live.
The error is shown on the datamodule page, and on reports as part of the stale
banner.

After a failure, **automatic** rebuilds (stale reads, and stale dependencies pulled
in by an automatic build) back off. They wait 1 minute, then 2, 4, … up to 1 hour
between attempts. Meanwhile reads get the previous version, marked stale.
**Manual and scheduled builds always run.** A successful build ends the back-off.

## Alerts

Each datamodule has an **alert list**: the people who hear when its data stops
refreshing. Set it in the **Alerts** section of the datamodule's page, one per line
(a username, an `@group` or an email address). Blank means the Strata admins.

| Event | Mail |
|---|---|
| A build fails | "build failing", with the error. **Once**, until the next success. |
| The next build succeeds | "recovered". |
| No successful build for longer than the **stale alert** (hours) | "data is stale". **Once**, until the next success. |
| A scheduled report or check run fails | the error (its recipients are not mailed). See [Checks and runs](checks.md#scheduled-runs-and-mail). |

A failing **gate check** fails the build, so it alerts like any other failure.

A build started with the **Build** button does not alert: whoever pressed it sees
the error on the page. A scheduled build, a rebuild for a stale read, a dependency
refreshed for another build, or the refresh before a scheduled run all alert.

The stale alert is off until you give it a number of hours. Set it a little above
the longest gap you expect between successful builds: for a datamodule rebuilt
every 15 minutes in business hours, 18 hours catches a dead night without mailing
every evening.

## Versions and retention

Every successful build is a new, immutable **version**. The datamodule page lists
them, with the live one marked. Each version links to each report, opened on that
version (see [Report URLs](urls.md#version-pinning)), and to the build script it was
built from (**script**; failed builds keep theirs too). A version's script goes when
the version does.

Retention removes old versions after each successful build:

| Policy | Keeps |
|---|---|
| count | the newest N versions; the live version is always kept |
| age | versions younger than the given number of seconds (blank = keep all) |
