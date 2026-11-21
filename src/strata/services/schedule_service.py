"""Schedules as RFC 5545 recurrence rules (RRULE), in a named timezone.

A schedule's definition is stored as JSON::

    {"kind": "weekly", "fields": {...the form...}, "timezone": "Australia/Brisbane",
     "rrule": "RRULE:FREQ=WEEKLY;INTERVAL=2;...", "dtstart": "2026-11-23T00:00"}

``rrule`` is the source of truth for when it runs: one or more ``RRULE:``
lines (RFC 5545, the format calendar programs use), evaluated by
python-dateutil from ``dtstart`` (local wall-clock time in ``timezone``, so
"07:00" stays 07:00 across daylight-saving changes). ``kind`` + ``fields``
are what the builder form produced, kept so the form can be edited again.
The ``rrule`` kind is the escape hatch: any RRULE text, for shapes the
builder does not offer.

Run times are computed in local time and stored/compared in UTC.

Performance: dateutil steps forward from ``dtstart``; a 15-minute rule
started 5 years ago takes ~250 ms per next-run computation (measured), which
runs once per scheduled run.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from strata.config import live_list

log = logging.getLogger(__name__)


DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
DAY_NAMES = {
    "MO": "Mon",
    "TU": "Tue",
    "WE": "Wed",
    "TH": "Thu",
    "FR": "Fri",
    "SA": "Sat",
    "SU": "Sun",
}
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR"]

KINDS = {
    "every": "Every N minutes or hours",
    "daily": "Daily at set times",
    "weekly": "Every N weeks on chosen days",
    "monthly": "Monthly on a day of the month",
    "working_day": "First or last working day of the month",
    "once": "Once",
    "rrule": "Custom rule (RRULE)",
}


class ScheduleError(ValueError):
    """The schedule form or rule is invalid (message is shown to the author)."""


@dataclass
class Definition:
    kind: str
    fields: dict[str, Any]
    timezone: str
    rrule: str
    dtstart: str  # local wall-clock "YYYY-MM-DDTHH:MM"

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "fields": self.fields,
                "timezone": self.timezone,
                "rrule": self.rrule,
                "dtstart": self.dtstart,
            }
        )

    @staticmethod
    def from_json(raw: str) -> Definition:
        data = json.loads(raw)
        return Definition(
            kind=str(data["kind"]),
            fields=dict(data.get("fields") or {}),
            timezone=str(data["timezone"]),
            rrule=str(data["rrule"]),
            dtstart=str(data["dtstart"]),
        )


# ---------------------------------------------------------------------------
# Timezones (the schedule.timezones setting)
# ---------------------------------------------------------------------------


def configured_timezones() -> list[str]:
    """The timezones offered to schedule authors, first = default; unknown names dropped."""
    valid = [n for n in live_list("schedule.timezones") if _zone_or_none(n) is not None]
    return valid or ["UTC"]


def _zone_or_none(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return None


def _zone(name: str) -> ZoneInfo:
    zone = _zone_or_none(name)
    if zone is None:
        raise ScheduleError(f"Unknown timezone '{name}'.")
    return zone


# ---------------------------------------------------------------------------
# Builder: form fields -> RRULE
# ---------------------------------------------------------------------------


def _time(raw: str, label: str) -> time:
    try:
        parsed = time.fromisoformat(raw.strip())
    except ValueError:
        raise ScheduleError(f"{label} must be a time like 07:30, not '{raw}'.") from None
    return parsed.replace(second=0, microsecond=0)


def _int(raw: Any, label: str, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ScheduleError(f"{label} must be a whole number.") from None
    if not low <= value <= high:
        raise ScheduleError(f"{label} must be between {low} and {high}.")
    return value


def _days(raw: Any, required: bool) -> list[str]:
    days = [d for d in DAYS if d in (raw or [])]
    if required and not days:
        raise ScheduleError("Choose at least one day.")
    return days


def _byday(days: list[str]) -> str:
    return f";BYDAY={','.join(days)}" if days and len(days) < 7 else ""


def _at(t: time) -> str:
    return f";BYHOUR={t.hour};BYMINUTE={t.minute};BYSECOND=0"


def _start(raw: str | None) -> str:
    """The anchor date for intervals (which weeks a fortnightly rule hits)."""
    if not raw:
        return date.today().isoformat() + "T00:00"
    try:
        return date.fromisoformat(raw.strip()).isoformat() + "T00:00"
    except ValueError:
        raise ScheduleError(f"Start date must be a date like 2026-11-23, not '{raw}'.") from None


def build(kind: str, fields: dict[str, Any], timezone: str) -> Definition:
    """Turn the builder form into a validated Definition."""
    _zone(timezone)
    if kind not in KINDS:
        raise ScheduleError(f"Unknown schedule type '{kind}'.")
    rules: list[str]
    dtstart = _start(fields.get("start_date"))

    if kind == "every":
        n = _int(fields.get("every_n", ""), "Interval", 1, 1440)
        unit = fields.get("every_unit", "minutes")
        if unit not in ("minutes", "hours"):
            raise ScheduleError("Interval unit must be minutes or hours.")
        freq = "MINUTELY" if unit == "minutes" else "HOURLY"
        rule = f"FREQ={freq};INTERVAL={n}" + _byday(_days(fields.get("days"), False))
        start_raw, end_raw = fields.get("window_start", ""), fields.get("window_end", "")
        if start_raw or end_raw:
            if not (start_raw and end_raw):
                raise ScheduleError("Give both ends of the time window, or neither.")
            start_t = _time(start_raw, "Window start")
            end_t = _time(end_raw, "Window end")
            if start_t.minute or end_t.minute:
                raise ScheduleError("The time window must start and end on the hour.")
            if end_t.hour <= start_t.hour:
                raise ScheduleError("The time window must end after it starts.")
            rule += ";BYHOUR=" + ",".join(str(h) for h in range(start_t.hour, end_t.hour))
        if freq == "HOURLY":
            rule += ";BYMINUTE=0"
        rules = [rule + ";BYSECOND=0"]

    elif kind == "daily":
        raw_times = [
            t for t in str(fields.get("times", "")).replace(";", ",").split(",") if t.strip()
        ]
        if not raw_times:
            raise ScheduleError("Give at least one time, e.g. 07:00, 13:30.")
        times = sorted({_time(t, "Time") for t in raw_times})
        byday = _byday(_days(fields.get("days"), False))
        # One rule per time: BYHOUR/BYMINUTE together would cross-multiply.
        rules = [f"FREQ=DAILY{byday}{_at(t)}" for t in times]

    elif kind == "weekly":
        n = _int(fields.get("every_weeks", "1"), "Weeks", 1, 52)
        days = _days(fields.get("days"), True)
        t = _time(str(fields.get("time", "")), "Time")
        rules = [f"FREQ=WEEKLY;INTERVAL={n};WKST=MO;BYDAY={','.join(days)}{_at(t)}"]

    elif kind == "monthly":
        n = _int(fields.get("every_months", "1"), "Months", 1, 12)
        t = _time(str(fields.get("time", "")), "Time")
        day_raw = str(fields.get("day", "")).strip().lower()
        if day_raw == "last":
            byday = "BYMONTHDAY=-1"
        else:
            day = _int(day_raw, "Day of month", 1, 31)
            # Day 29-31 falls back to the month's last day in shorter months.
            byday = (
                f"BYMONTHDAY={','.join(str(d) for d in range(28, day + 1))};BYSETPOS=-1"
                if day > 28
                else f"BYMONTHDAY={day}"
            )
        rules = [f"FREQ=MONTHLY;INTERVAL={n};{byday}{_at(t)}"]

    elif kind == "working_day":
        which = fields.get("which", "first")
        if which not in ("first", "last"):
            raise ScheduleError("Choose the first or the last working day.")
        t = _time(str(fields.get("time", "")), "Time")
        pos = 1 if which == "first" else -1
        rules = [f"FREQ=MONTHLY;BYDAY={','.join(WEEKDAYS)};BYSETPOS={pos}{_at(t)}"]

    elif kind == "once":
        raw = str(fields.get("at", "")).strip()
        try:
            at = datetime.fromisoformat(raw).replace(second=0, microsecond=0, tzinfo=None)
        except ValueError:
            raise ScheduleError(f"Run time must be a date and time, not '{raw}'.") from None
        dtstart = at.isoformat(timespec="minutes")
        rules = ["FREQ=DAILY;COUNT=1"]

    else:  # rrule: the escape hatch
        text = str(fields.get("rrule", "")).strip()
        if not text:
            raise ScheduleError("Enter an RRULE, e.g. FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=0.")
        rules = [line.strip().removeprefix("RRULE:") for line in text.splitlines() if line.strip()]

    definition = Definition(
        kind=kind,
        fields=fields,
        timezone=timezone,
        rrule="\n".join(f"RRULE:{r}" for r in rules),
        dtstart=dtstart,
    )
    if not upcoming(definition, datetime.now(UTC), 1):
        raise ScheduleError("This schedule has no future runs.")
    return definition


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _ruleset(definition: Definition) -> Any:
    zone = _zone(definition.timezone)
    start = datetime.fromisoformat(definition.dtstart).replace(tzinfo=zone)
    try:
        return rrulestr(definition.rrule, dtstart=start, forceset=True)
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"Invalid rule: {exc}") from exc


def next_run(definition: Definition, after: datetime) -> datetime | None:
    """The first run strictly after ``after`` (aware), in UTC; None if no more runs."""
    found = _ruleset(definition).after(after.astimezone(_zone(definition.timezone)))
    return found.astimezone(UTC) if found is not None else None


def upcoming(definition: Definition, after: datetime, n: int = 5) -> list[datetime]:
    """The next ``n`` runs after ``after``, in UTC."""
    runs: list[datetime] = []
    current = after
    for _ in range(n):
        nxt = next_run(definition, current)
        if nxt is None:
            break
        runs.append(nxt)
        current = nxt
    return runs


def describe(definition: Definition) -> str:
    """A one-line human description of what the builder form said."""
    f = definition.fields
    days = [DAY_NAMES[d] for d in DAYS if d in (f.get("days") or [])]
    on_days = f" on {', '.join(days)}" if days and len(days) < 7 else ""
    match definition.kind:
        case "every":
            window = f", {f['window_start']}-{f['window_end']}" if f.get("window_start") else ""
            return f"Every {f.get('every_n')} {f.get('every_unit')}{on_days}{window}"
        case "daily":
            return f"Daily at {f.get('times')}{on_days}"
        case "weekly":
            n = int(f.get("every_weeks") or 1)
            every = "Weekly" if n == 1 else f"Every {n} weeks"
            return (
                f"{every} on {', '.join(days)} at {f.get('time')} (from {definition.dtstart[:10]})"
            )
        case "monthly":
            n = int(f.get("every_months") or 1)
            every = "Monthly" if n == 1 else f"Every {n} months"
            return f"{every} on day {f.get('day')} at {f.get('time')}"
        case "working_day":
            return f"{str(f.get('which', '')).capitalize()} working day of the month at {f.get('time')}"
        case "once":
            return f"Once at {definition.dtstart.replace('T', ' ')}"
        case _:
            return definition.rrule.replace("\n", " ")


# ---------------------------------------------------------------------------
# The worker's scheduler tick
# ---------------------------------------------------------------------------


def run_due(now: datetime | None = None) -> list[str]:
    """Queue a forced build for every enabled schedule that is due; returns their names.

    Each schedule's next run is computed from ``now``, so runs missed while no
    worker was up collapse into one. ``claim_run`` is a compare-and-set, so two
    workers ticking together queue it once (and the job queue coalesces anyway).
    """
    from strata.models.datamodule import Datamodule
    from strata.models.schedule import Schedule
    from strata.services import build_queue

    now = now or datetime.now(UTC)
    fired: list[str] = []
    for schedule in Schedule.due(now.isoformat()):
        dm = Datamodule.get_by_id(schedule.datamodule_id)
        if dm is None:
            continue
        try:
            nxt = next_run(Definition.from_json(schedule.schedule_definition), now)
        except (ScheduleError, ValueError, KeyError) as exc:
            log.warning(
                "schedule %s (%s) is invalid, disabling it: %s", schedule.name, dm.path, exc
            )
            schedule.set_enabled(False, None)
            continue
        if not schedule.claim_run(now.isoformat(), nxt.isoformat() if nxt else None):
            continue
        build_queue.request_build(dm, f"schedule:{schedule.name}", force=True)
        fired.append(f"{dm.path}: {schedule.name}")
    return fired
