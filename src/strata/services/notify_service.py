"""Mail for scheduled runs. It never raises: a mail problem is recorded on the run.

Who gets what (the 2026-11-21 decisions):

- a **check** run that finds rows mails its schedule's recipients; a check with
  no rows sends nothing;
- a plain **report** run always mails its recipients;
- a run whose **query failed** mails the alert list (Strata admins) only.

Recipients are Gatekeeper usernames, ``@group`` names, or plain email
addresses. A name that cannot be resolved (unknown, disabled, no email) is
skipped and noted on the run.

The mail carries the count, "data as of" (with any refresh problem on the same
line), the first rows inline, and a link to the saved run: no attachment. Mail about
a restricted datamodule carries neither rows nor error texts (``error_html``). Mail
goes through Outbox; tests replace the sender with ``app.config["NOTIFY_SINK"]``
(a callable taking a ``Mail``).
"""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app
from markupsafe import Markup, escape

from strata.config import live_int, live_str
from strata.models.datamodule import Datamodule
from strata.models.report import Report
from strata.models.report_check import ReportCheck
from strata.models.report_run import ReportRun
from strata.models.schedule import Schedule

log = logging.getLogger(__name__)

INLINE_ROWS = 20


@dataclass
class Mail:
    to: list[str]
    subject: str
    html: str


class MailNotConfigured(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# What mail may quote
# ---------------------------------------------------------------------------


def quotes_errors(dm: Datamodule) -> bool:
    """Whether mail about ``dm`` may quote an error text.

    An error can be data: a failed gate check's error lists its first rows, and
    DuckDB quotes the values it cannot convert. Mail bodies sit in Outbox's shared
    database, so mail about a restricted datamodule (viewers not everyone) quotes no
    error, as it carries no rows (user decision 2026-11-23)."""
    from strata.services import access

    return not access.is_restricted(dm)


def _not_quoted(dm: Datamodule) -> str:
    return f"not quoted here, as who may read {dm.path} is restricted: open Strata to read it"


def error_html(dm: Datamodule, error: str, lead: str = "Error: ") -> Markup:
    """One paragraph giving an error in mail about ``dm``: the text itself, or for a
    restricted datamodule only that it is in Strata."""
    if quotes_errors(dm):
        return Markup("<p>{}<code>{}</code></p>").format(lead, error)
    return Markup("<p>{}{}.</p>").format(lead, _not_quoted(dm))


def data_note_text(dm: Datamodule, note: str) -> str:
    """A run's ``data_note`` for its mail: a failed refresh keeps its words but, for a
    restricted datamodule, not its error."""
    from strata.services.run_service import REFRESH_FAILED

    if note.startswith(REFRESH_FAILED) and not quotes_errors(dm):
        return f"{REFRESH_FAILED}{_not_quoted(dm)}"
    return note


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _outbox_send(mail: Mail) -> None:
    from outbox import Message, OutboxClient

    db_path = live_str("outbox.db_path")
    url, api_key = live_str("outbox.url"), live_str("outbox.api_key")
    if db_path:
        client = OutboxClient(db_path=db_path)
    elif url and api_key:
        client = OutboxClient(server_url=url, api_key=api_key)
    else:
        raise MailNotConfigured(
            "Outbox is not configured: set outbox.db_path, or outbox.url and outbox.api_key"
        )
    client.submit_message(
        Message(
            from_address=live_str("mail.sender"),
            to=mail.to,
            subject=mail.subject,
            body=mail.html,
            body_type="html",
            source_app="strata",
        )
    )


def _sender() -> Callable[[Mail], None]:
    return current_app.config.get("NOTIFY_SINK") or _outbox_send


def send(mail: Mail) -> str:
    """Send ``mail`` (Outbox, or the test sink); returns when. Raises on failure."""
    _sender()(mail)
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

# A recipient line: an @group, an email address, or a Gatekeeper username.
# A username may carry its domain: Gatekeeper's look like asiap\pawe.
RECIPIENT_RE = re.compile(r"^(@[\w.\- ]+|[^@\s]+@[^@\s]+\.[^@\s]+|(?:[\w.\-]+\\)?[\w.\-]+)$")


def recipient_lines(text: str) -> list[str]:
    """The non-blank lines of a recipients textarea."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def check_recipients(entries: list[str]) -> str | None:
    """Why the first malformed entry is refused, or None."""
    for entry in entries:
        if not RECIPIENT_RE.match(entry):
            return f"'{entry}' is not a username, an @group or an email address."
    return None


def resolve_recipients(entries: Iterable[str]) -> tuple[list[str], list[str]]:
    """(email addresses, problems). Duplicates are dropped, ignoring case."""
    gk = current_app.config.get("GATEKEEPER_CLIENT")
    emails: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()

    def add(email: str) -> None:
        if email.lower() not in seen:
            seen.add(email.lower())
            emails.append(email)

    def user_email(username: str, via: str = "") -> str | None:
        label = f"{username} (in {via})" if via else username
        try:
            user = gk.get_user(username) if gk is not None else None
        except Exception as exc:  # noqa: BLE001 -- a lookup failure is a note, not a crash
            problems.append(f"{label}: lookup failed ({exc})")
            return None
        if user is None:
            problems.append(f"{label}: no such user")
        elif not user.enabled:
            problems.append(f"{label}: disabled")
        elif not user.email:
            problems.append(f"{label}: no email address")
        else:
            return str(user.email)
        return None

    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        if "@" in entry and not entry.startswith("@"):
            add(entry)
            continue
        if gk is None:
            problems.append(f"{entry}: Gatekeeper is not configured, so names cannot be looked up")
            continue
        if entry.startswith("@"):
            try:
                members = gk.get_group_members(entry[1:])
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{entry}: lookup failed ({exc})")
                continue
            if not members:
                problems.append(f"{entry}: no such group, or it has no members")
            for username in members:
                email = user_email(username, via=entry)
                if email:
                    add(email)
        else:
            email = user_email(entry)
            if email:
                add(email)
    return emails, problems


# ---------------------------------------------------------------------------
# The mail
# ---------------------------------------------------------------------------


def _local(iso: str | None, timezone: str) -> str:
    if not iso:
        return "unknown"
    when = datetime.fromisoformat(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(ZoneInfo(timezone)).strftime("%a %d %b %Y %H:%M %Z")


def run_link(run: ReportRun) -> str | None:
    base = live_str("server.public_url").rstrip("/")
    return f"{base}/runs/{run.uuid}" if base else None


def _first_rows(run: ReportRun, dm: Datamodule) -> tuple[list[str], list[tuple[Any, ...]]]:
    from strata.services import duck, run_service

    path = run_service.snapshot_file(dm.uuid, run)
    if path is None or not path.exists():
        return [], []
    conn = duck.snapshot_conn(path)
    try:
        cursor = conn.execute(f"SELECT * FROM snapshot LIMIT {INLINE_ROWS}")
        rows = cursor.fetchall()
        columns = [str(d[0]) for d in cursor.description or []]
    finally:
        conn.close()
    return columns, rows


def _table(columns: list[str], rows: list[tuple[Any, ...]]) -> Markup:
    cell = "border:1px solid #ccc;padding:3px 6px;text-align:left;"
    head = "".join(f'<th style="{cell}background:#f3f3f3;">{escape(c)}</th>' for c in columns)
    body = "".join(
        "<tr>"
        + "".join(f'<td style="{cell}">{escape("" if v is None else v)}</td>' for v in row)
        + "</tr>"
        for row in rows
    )
    return Markup(
        f'<table style="border-collapse:collapse;font-size:13px;"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _rows_word(n: int) -> str:
    return f"{n:,} row{'' if n == 1 else 's'}"


def build_run_mail(
    run: ReportRun, report: Report, dm: Datamodule, schedule: Schedule | None, to: list[str]
) -> Mail:
    """The mail for a run that found rows (or any run of a plain report)."""
    check = ReportCheck.get(report.id)
    count = run.row_count or 0
    prefix = f"[{check.severity.upper()}] " if check else ""
    subject = f"{prefix}{report.name} — {_rows_word(count)}"

    timezone = _schedule_timezone(schedule)
    first = f"Data as of {_local(run.data_as_of, timezone)}"
    if run.data_note:
        first += f"; {data_note_text(dm, run.data_note)}"
    from strata.services import access

    if access.is_restricted(dm):
        # Mail bodies sit in Outbox's shared database: rows of restricted data stay in
        # Strata, behind its viewers list (user decision 2026-11-23).
        columns, rows = [], []
        shown = (
            f"{_rows_word(count)}. Who may read {dm.path} is restricted, so the rows are "
            "not in this mail: open the run in Strata."
        )
    else:
        columns, rows = _first_rows(run, dm)
        shown = (
            f"The first {len(rows)} of {_rows_word(count)}:"
            if count > len(rows)
            else f"{_rows_word(count)}:"
        )
    link = run_link(run)
    days = live_int("runs.retention_days")
    if link:
        more = Markup(
            '<p><a href="{}">Open the run: all {} with sorting and downloads</a> (kept {} days).</p>'
        ).format(link, _rows_word(count), days)
    else:
        more = Markup(
            "<p>The run is saved in Strata as run <code>{}</code> (kept {} days).</p>"
        ).format(run.uuid, days)
    footer = _footer(report, dm, schedule, run)
    html = Markup(
        '<div style="font-family:sans-serif;font-size:14px;">'
        "<p>{first}.</p><p><strong>{shown}</strong></p>{table}{more}{footer}</div>"
    ).format(
        first=first,
        shown=shown,
        table=_table(columns, rows) if rows else "",
        more=more,
        footer=footer,
    )
    return Mail(to=to, subject=subject, html=str(html))


def build_failure_mail(
    run: ReportRun, report: Report, dm: Datamodule, schedule: Schedule | None, to: list[str]
) -> Mail:
    link = run_link(run)
    html = Markup(
        '<div style="font-family:sans-serif;font-size:14px;">'
        "<p>The scheduled run of <strong>{name}</strong> failed, so its recipients were not "
        "mailed.</p>{error}{link}{footer}</div>"
    ).format(
        name=report.name,
        error=error_html(dm, run.error or "unknown error"),
        link=Markup('<p><a href="{}">Open the run</a></p>').format(link) if link else "",
        footer=_footer(report, dm, schedule, run),
    )
    return Mail(to=to, subject=f"[FAILED] {report.name} — the scheduled run failed", html=str(html))


def _schedule_timezone(schedule: Schedule | None) -> str:
    """Times in a mail are shown in the schedule's own timezone."""
    from strata.services.schedule_service import Definition

    if schedule is not None:
        try:
            return Definition.from_json(schedule.schedule_definition).timezone or "UTC"
        except ValueError, KeyError:
            pass
    return "UTC"


def _footer(report: Report, dm: Datamodule, schedule: Schedule | None, run: ReportRun) -> Markup:
    args = ", ".join(f"{k}={','.join(v)}" for k, v in sorted(run.args.items())) or "defaults"
    where = f"schedule “{schedule.name}”" if schedule else f"run by {run.triggered_by}"
    return Markup(
        '<p style="color:#666;font-size:12px;">{report} on {dm} · {where} · arguments: {args}</p>'
    ).format(report=report.name, dm=dm.path, where=where, args=args)


# ---------------------------------------------------------------------------
# Deciding and recording
# ---------------------------------------------------------------------------


def notify_run(run: ReportRun, report: Report, dm: Datamodule, schedule: Schedule | None) -> None:
    """Mail a scheduled run as the rules above say, and record what happened on
    the run (``notified_at``, ``notify_note``). Never raises."""
    try:
        notified_at, note = _notify(run, report, dm, schedule)
    except Exception as exc:  # noqa: BLE001 -- the webreports lesson: mail never breaks a run
        log.exception("mail for run %s of %s failed", run.uuid, report.name)
        notified_at, note = None, f"mail not sent: {exc}"
    try:
        run.set_notified(notified_at, note)
    except Exception:  # noqa: BLE001
        log.exception("could not record the mail outcome of run %s", run.uuid)


def _notify(
    run: ReportRun, report: Report, dm: Datamodule, schedule: Schedule | None
) -> tuple[str | None, str]:
    if run.status == "error":
        from strata.services.alert_service import alert_list

        to, problems = resolve_recipients(alert_list(dm))
        if not to:
            return None, "query failed; the alert list could not be resolved: " + "; ".join(
                problems
            )
        sent = send(build_failure_mail(run, report, dm, schedule, to))
        return sent, f"query failed: mailed the alert list only ({', '.join(to)})"

    if ReportCheck.get(report.id) is not None and not run.row_count:
        return None, "no rows: a check that finds nothing sends no mail"

    entries = schedule.recipients if schedule else []
    if not entries:
        return None, "no mail: the schedule has no recipients"
    to, problems = resolve_recipients(entries)
    skipped = f"; not resolved: {'; '.join(problems)}" if problems else ""
    if problems:
        log.warning("run %s of %s: unresolved recipients: %s", run.uuid, report.name, problems)
    if not to:
        return None, f"no mail: no recipient could be resolved{skipped}"
    sent = send(build_run_mail(run, report, dm, schedule, to))
    return sent, f"mailed {', '.join(to)}{skipped}"
