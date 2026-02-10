"""Schedule service — pure Python next-run calculation with business day awareness."""

import calendar
from datetime import datetime, time, timedelta
from typing import Any


def next_run(definition: dict[str, Any], after: datetime) -> datetime | None:
    """Calculate the next run time after the given datetime.

    Returns None for one-time schedules that have already passed.
    """
    match definition["type"]:
        case "interval":
            return _next_interval(definition, after)
        case "daily":
            return _next_daily(definition, after)
        case "weekly":
            return _next_weekly(definition, after)
        case "monthly_day":
            return _next_monthly_day(definition, after)
        case "monthly_pattern":
            return _next_monthly_pattern(definition, after)
        case "one_time":
            return _next_one_time(definition, after)
        case _:
            return None


def next_n_runs(definition: dict[str, Any], n: int = 5) -> list[datetime]:
    """Calculate the next N run times from now."""
    from datetime import UTC

    results: list[datetime] = []
    current = datetime.now(UTC)

    for _ in range(n):
        nxt = next_run(definition, current)
        if nxt is None:
            break
        results.append(nxt)
        current = nxt + timedelta(seconds=1)

    return results


def _parse_time(time_str: str) -> time:
    """Parse a time string like '08:00' into a time object."""
    parts = time_str.split(":")
    return time(int(parts[0]), int(parts[1]))


def _next_interval(definition: dict[str, Any], after: datetime) -> datetime:
    every = definition["every"]
    unit = definition["unit"]

    match unit:
        case "minutes":
            delta = timedelta(minutes=every)
        case "hours":
            delta = timedelta(hours=every)
        case "days":
            delta = timedelta(days=every)
        case _:
            delta = timedelta(hours=every)

    candidate = after + delta

    # If an 'at' time is specified for daily intervals, snap to that time
    if "at" in definition and unit == "days":
        t = _parse_time(definition["at"])
        candidate = candidate.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)

    return candidate


def _next_daily(definition: dict[str, Any], after: datetime) -> datetime:
    at_value = definition["at"]
    times = at_value if isinstance(at_value, list) else [at_value]

    candidates: list[datetime] = []
    for time_str in times:
        t = _parse_time(time_str)
        candidate = after.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        candidates.append(candidate)

    return min(candidates)


def _next_weekly(definition: dict[str, Any], after: datetime) -> datetime:
    days = definition["days"]
    t = _parse_time(definition["at"])

    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    target_weekdays = sorted(day_names.index(d.lower()) for d in days)

    if not target_weekdays:
        return after + timedelta(days=7)

    current_weekday = after.weekday()
    candidate_today = after.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

    # Check today and next 7 days
    for day_offset in range(8):
        check_day = (current_weekday + day_offset) % 7
        if check_day in target_weekdays:
            candidate = candidate_today + timedelta(days=day_offset)
            if candidate > after:
                return candidate

    # Fallback — should not reach here
    return candidate_today + timedelta(days=7)


def _next_monthly_day(definition: dict[str, Any], after: datetime) -> datetime:
    day = definition["day"]
    t = _parse_time(definition["at"])

    year = after.year
    month = after.month

    for _ in range(13):  # Check up to 13 months ahead
        last_day = calendar.monthrange(year, month)[1]

        if day == -1:
            target_day = last_day
        else:
            target_day = min(day, last_day)

        candidate = datetime(year, month, target_day, t.hour, t.minute, tzinfo=after.tzinfo)
        if candidate > after:
            return candidate

        # Move to next month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1

    return after + timedelta(days=31)


def _first_working_day(year: int, month: int) -> int:
    """Find the first working day (Mon-Fri) of a month."""
    day = 1
    while True:
        weekday = calendar.weekday(year, month, day)
        if weekday < 5:  # Monday=0 through Friday=4
            return day
        day += 1


def _last_working_day(year: int, month: int) -> int:
    """Find the last working day (Mon-Fri) of a month."""
    last_day = calendar.monthrange(year, month)[1]
    day = last_day
    while day > 0:
        weekday = calendar.weekday(year, month, day)
        if weekday < 5:
            return day
        day -= 1
    return last_day  # Fallback


def _next_monthly_pattern(definition: dict[str, Any], after: datetime) -> datetime:
    pattern = definition["pattern"]
    t = _parse_time(definition["at"])

    year = after.year
    month = after.month

    for _ in range(13):
        match pattern:
            case "first_working_day":
                target_day = _first_working_day(year, month)
            case "last_working_day":
                target_day = _last_working_day(year, month)
            case "first_day":
                target_day = 1
            case "last_day":
                target_day = calendar.monthrange(year, month)[1]
            case _:
                target_day = 1

        candidate = datetime(year, month, target_day, t.hour, t.minute, tzinfo=after.tzinfo)
        if candidate > after:
            return candidate

        if month == 12:
            year += 1
            month = 1
        else:
            month += 1

    return after + timedelta(days=31)


def _next_one_time(definition: dict[str, Any], after: datetime) -> datetime | None:
    dt_str = definition["datetime"]
    target = datetime.fromisoformat(dt_str)
    if target.tzinfo is None and after.tzinfo is not None:
        target = target.replace(tzinfo=after.tzinfo)

    if target > after:
        return target
    return None
