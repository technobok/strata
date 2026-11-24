"""Alerts: tell a datamodule's alert list when its data stops refreshing.

- **Build failing**: the first failed build since the last success mails the
  list once (``failure_alerted_at``). A build started with the Build button
  does not alert: its author is looking at the error. Gate checks fail builds
  too, so a gate that finds rows alerts here.
- **Recovered**: the next successful build after a "failing" mail says so.
- **Stale**: when a datamodule's last successful build is older than its
  ``stale_alert_seconds``, the worker's sweep mails once; a success re-arms it.
- **Reports broken**: a successful build after which a report that ran no longer
  does (``report_service.check_reports``) mails the reports and their errors, once
  per report until it runs again (not for the Build button).

The list is the datamodule's ``alert_recipients`` (usernames, @groups, email
addresses), or the Strata admins when unset. A failed report/check query is
mailed to the same list (notify_service). Alerts never raise: a build's own
outcome never depends on mail.
"""

import logging
from datetime import UTC, datetime, timedelta

from flask import current_app
from markupsafe import Markup

from strata.config import live_str
from strata.models.datamodule import Datamodule
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.report import Report

log = logging.getLogger(__name__)


def alert_list(dm: Datamodule) -> list[str]:
    if dm.alert_recipients:
        return dm.alert_recipients
    return [f"@{current_app.config.get('AUTH_ADMIN_GROUP') or 'strata-admins'}"]


def _page_link(dm: Datamodule) -> Markup:
    base = live_str("server.public_url").rstrip("/")
    if not base:
        return Markup("")
    return Markup('<p><a href="{}">Open {} in Strata</a></p>').format(
        f"{base}/datamodules/{dm.uuid}/edit", dm.path
    )


def _mail(dm: Datamodule, subject: str, body: Markup) -> bool:
    """Send to the alert list; True if a mail went out. Never raises."""
    from strata.services import notify_service

    try:
        to, problems = notify_service.resolve_recipients(alert_list(dm))
        if problems:
            log.warning("alert for %s: unresolved recipients: %s", dm.path, problems)
        if not to:
            log.warning("alert for %s not sent: nobody on the alert list resolved", dm.path)
            return False
        html = Markup('<div style="font-family:sans-serif;font-size:14px;">{}{}</div>').format(
            body, _page_link(dm)
        )
        notify_service.send(notify_service.Mail(to=to, subject=subject, html=str(html)))
        return True
    except Exception:  # noqa: BLE001 -- an alert never changes a build's outcome
        log.exception("alert for %s could not be sent", dm.path)
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _when(iso: str | None) -> str:
    if not iso:
        return "never"
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%a %d %b %Y %H:%M UTC")


def build_failed(dm_id: int, triggered_by: str, error: str) -> None:
    """After a failed build: alert once per run of failures (not for the Build button)."""
    from strata.services import notify_service

    try:
        dm = Datamodule.get_by_id(dm_id)
        if dm is None or dm.failure_alerted_at is not None or triggered_by.startswith("manual:"):
            return
        live = DatamoduleVersion.get_by_id(dm.live_version_id) if dm.live_version_id else None
        serving = (
            f"Reports keep serving the previous version, built {_when(live.build_ended_at)}."
            if live
            else "It has never been built, so its reports have no data."
        )
        body = Markup(
            "<p>The build of <strong>{path}</strong> failed ({trigger}).</p>"
            "{error}<p>{serving} Automatic rebuilds retry with back-off; "
            "scheduled and manual builds always run. You will get one mail when it recovers.</p>"
        ).format(
            path=dm.path,
            trigger=triggered_by,
            error=notify_service.error_html(dm, error),
            serving=serving,
        )
        if _mail(dm, f"[STRATA] {dm.path}: build failing", body):
            dm.mark_alerted("failure_alerted_at", _now())
    except Exception:  # noqa: BLE001
        log.exception("build-failure alert for datamodule %s failed", dm_id)


def access_locked(dm_id: int, triggered_by: str, reason: str) -> None:
    """A build found that an owner may no longer read a parent or use a connection:
    the datamodule is locked. Mail the list once, whoever started the build (a lock
    matters even after a manual build); "recovered" follows the next success."""
    try:
        dm = Datamodule.get_by_id(dm_id)
        if dm is None:
            return
        body = Markup(
            "<p><strong>{path}</strong> is <strong>locked</strong> ({trigger}): it serves "
            "nothing (pages, runs, data links, Excel links) until its owners or an admin fix "
            "its access and a build succeeds.</p><p><code>{reason}</code></p>"
            "<p>Fix it on its page: change its owners, or ask for access to what it reads.</p>"
        ).format(path=dm.path, trigger=triggered_by, reason=reason)
        if _mail(dm, f"[STRATA] {dm.path}: locked (access)", body):
            dm.mark_alerted("failure_alerted_at", _now())
    except Exception:  # noqa: BLE001
        log.exception("access-lock alert for datamodule %s failed", dm_id)


def reports_broken(dm_id: int, triggered_by: str, broken: list[tuple[Report, str]]) -> None:
    """After a successful build: the reports that ran before it and no longer do.
    Not for the Build button (its author sees them on the datamodule page)."""
    from strata.services import notify_service

    try:
        dm = Datamodule.get_by_id(dm_id)
        if dm is None or not broken or triggered_by.startswith("manual:"):
            return
        items = Markup("").join(
            Markup("<li><strong>{name}</strong>{error}</li>").format(
                name=report.name, error=notify_service.error_html(dm, problem, lead="")
            )
            for report, problem in broken
        )
        n = len(broken)
        body = Markup(
            "<p><strong>{path}</strong> built successfully ({trigger}), but {n} of its "
            "report{s} no longer run{verb} on the new data: every page, run, schedule and "
            "Excel link of {them} fails until {it} or the build script is fixed.</p>"
            "<ul>{items}</ul>"
        ).format(
            path=dm.path,
            trigger=triggered_by,
            n=n,
            s="" if n == 1 else "s",
            verb="s" if n == 1 else "",
            them="it" if n == 1 else "them",
            it="the report" if n == 1 else "the reports",
            items=items,
        )
        _mail(dm, f"[STRATA] {dm.path}: {n} report{'' if n == 1 else 's'} broken", body)
    except Exception:  # noqa: BLE001
        log.exception("broken-reports alert for datamodule %s failed", dm_id)


def build_succeeded(dm_id: int, failures: int) -> None:
    """After a successful build: "recovered" if "failing" was mailed; re-arm stale."""
    try:
        dm = Datamodule.get_by_id(dm_id)
        if dm is None:
            return
        if dm.stale_alerted_at is not None:
            dm.mark_alerted("stale_alerted_at", None)
        if dm.failure_alerted_at is None:
            return
        body = Markup(
            "<p><strong>{path}</strong> built successfully at {when}, after {n} failed "
            "build{s}. Its reports serve fresh data again.</p>"
        ).format(path=dm.path, when=_when(_now()), n=failures, s="" if failures == 1 else "s")
        _mail(dm, f"[STRATA] {dm.path}: recovered", body)
        dm.mark_alerted("failure_alerted_at", None)
    except Exception:  # noqa: BLE001
        log.exception("recovery alert for datamodule %s failed", dm_id)


def sweep_stale(now: datetime | None = None) -> list[str]:
    """Mail each datamodule whose data is older than its stale_alert_seconds (once
    until its next success). Returns the paths alerted."""
    from strata.services import notify_service

    now = now or datetime.now(UTC)
    alerted: list[str] = []
    for dm in Datamodule.get_all():
        if dm.stale_alert_seconds is None or dm.stale_alerted_at is not None:
            continue
        live = DatamoduleVersion.get_by_id(dm.live_version_id) if dm.live_version_id else None
        since = live.build_ended_at if live and live.build_ended_at else dm.created_at
        if now - datetime.fromisoformat(since) <= timedelta(seconds=dm.stale_alert_seconds):
            continue
        hours = dm.stale_alert_seconds / 3600
        body = Markup(
            "<p><strong>{path}</strong> has had no successful build since {since}: longer "
            "than its stale alert of {hours:g} hour{s}.</p>{error}"
        ).format(
            path=dm.path,
            since=_when(live.build_ended_at) if live else "it was created (never built)",
            hours=hours,
            s="" if hours == 1 else "s",
            error=notify_service.error_html(dm, dm.last_build_error, "Last build error: ")
            if dm.last_build_error
            else "",
        )
        if _mail(dm, f"[STRATA] {dm.path}: data is stale", body):
            dm.mark_alerted("stale_alerted_at", now.isoformat())
            alerted.append(dm.path)
    return alerted
