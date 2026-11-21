"""Schedules: RRULE building, timezones, the form, and the worker's tick."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from strata.services import schedule_service
from strata.services.schedule_service import ScheduleError
from tests.conftest import Strata

BNE = "Australia/Brisbane"


def _runs(kind: str, fields: dict, tz: str, after_local: str, n: int = 4) -> list[str]:
    # A schedule starts on its start date (default: the day it is saved), so
    # these fixed-clock examples start well before their "after" times.
    fields = {"start_date": "2026-01-01", **fields}
    definition = schedule_service.build(kind, fields, tz)
    zone = ZoneInfo(tz)
    after = datetime.fromisoformat(after_local).replace(tzinfo=zone)
    return [
        r.astimezone(zone).strftime("%a %d %b %H:%M %Z")
        for r in schedule_service.upcoming(definition, after, n)
    ]


# -- rule shapes ----------------------------------------------------------------


def test_every_15_minutes_in_business_hours() -> None:
    fields = {
        "every_n": "15",
        "every_unit": "minutes",
        "days": ["MO", "TU", "WE", "TH", "FR"],
        "window_start": "06:00",
        "window_end": "20:00",
    }
    assert _runs("every", fields, BNE, "2026-11-20T19:31") == [
        "Fri 20 Nov 19:45 AEST",
        "Mon 23 Nov 06:00 AEST",
        "Mon 23 Nov 06:15 AEST",
        "Mon 23 Nov 06:30 AEST",
    ]


def test_every_2_hours_within_a_window() -> None:
    fields = {"every_n": "2", "every_unit": "hours", "window_start": "08:00", "window_end": "17:00"}
    assert _runs("every", fields, BNE, "2026-11-20T09:00", 5) == [
        "Fri 20 Nov 10:00 AEST",
        "Fri 20 Nov 12:00 AEST",
        "Fri 20 Nov 14:00 AEST",
        "Fri 20 Nov 16:00 AEST",
        "Sat 21 Nov 08:00 AEST",
    ]


def test_daily_at_several_times_does_not_cross_multiply() -> None:
    assert _runs("daily", {"times": "13:30, 07:00"}, BNE, "2026-11-20T08:00", 3) == [
        "Fri 20 Nov 13:30 AEST",
        "Sat 21 Nov 07:00 AEST",
        "Sat 21 Nov 13:30 AEST",
    ]


def test_every_two_weeks_is_anchored_on_the_start_date() -> None:
    fields = {"every_weeks": "2", "days": ["MO"], "time": "07:00", "start_date": "2026-11-23"}
    assert _runs("weekly", fields, "Australia/Sydney", "2026-11-23T08:00", 3) == [
        "Mon 07 Dec 07:00 AEDT",
        "Mon 21 Dec 07:00 AEDT",
        "Mon 04 Jan 07:00 AEDT",
    ]


def test_monthly_day_31_falls_back_to_the_last_day() -> None:
    assert _runs("monthly", {"day": "31", "time": "18:00"}, BNE, "2027-01-15T00:00", 3) == [
        "Sun 31 Jan 18:00 AEST",
        "Sun 28 Feb 18:00 AEST",
        "Wed 31 Mar 18:00 AEST",
    ]
    assert _runs("monthly", {"day": "last", "time": "23:00"}, BNE, "2028-02-01T00:00", 1) == [
        "Tue 29 Feb 23:00 AEST"
    ]


@pytest.mark.parametrize(
    ("which", "expected"),
    [
        ("first", ["Mon 02 Nov 06:00 AEST", "Tue 01 Dec 06:00 AEST"]),
        ("last", ["Fri 30 Oct 06:00 AEST", "Mon 30 Nov 06:00 AEST"]),
    ],
)
def test_first_and_last_working_day(which: str, expected: list[str]) -> None:
    assert (
        _runs("working_day", {"which": which, "time": "06:00"}, BNE, "2026-10-15T00:00", 2)
        == expected
    )


def test_daily_0700_auckland_stays_0700_across_daylight_saving() -> None:
    assert _runs("daily", {"times": "07:00"}, "Pacific/Auckland", "2026-09-25T08:00") == [
        "Sat 26 Sep 07:00 NZST",
        "Sun 27 Sep 07:00 NZDT",
        "Mon 28 Sep 07:00 NZDT",
        "Tue 29 Sep 07:00 NZDT",
    ]


def test_once_runs_once() -> None:
    assert _runs("once", {"at": "2027-03-01T09:30"}, BNE, "2026-11-20T00:00") == [
        "Mon 01 Mar 09:30 AEST"
    ]


def test_a_custom_rrule_is_the_escape_hatch() -> None:
    second_tuesday = {"rrule": "FREQ=MONTHLY;BYDAY=TU;BYSETPOS=2;BYHOUR=9;BYMINUTE=0;BYSECOND=0"}
    assert _runs("rrule", second_tuesday, BNE, "2026-11-01T00:00", 2) == [
        "Tue 10 Nov 09:00 AEST",
        "Tue 08 Dec 09:00 AEST",
    ]


@pytest.mark.parametrize(
    ("kind", "fields", "tz", "message"),
    [
        (
            "every",
            {
                "every_n": "15",
                "every_unit": "minutes",
                "window_start": "06:30",
                "window_end": "20:00",
            },
            BNE,
            "on the hour",
        ),
        (
            "every",
            {
                "every_n": "15",
                "every_unit": "minutes",
                "window_start": "20:00",
                "window_end": "06:00",
            },
            BNE,
            "end after",
        ),
        ("every", {"every_n": "0", "every_unit": "minutes"}, BNE, "between 1"),
        ("weekly", {"every_weeks": "1", "time": "07:00"}, BNE, "at least one day"),
        ("daily", {"times": "7am"}, BNE, "time like"),
        ("daily", {"times": "07:00"}, "Mars/Olympus", "Unknown timezone"),
        ("rrule", {"rrule": "FREQ=SOMETIMES"}, BNE, "Invalid rule"),
        ("rrule", {"rrule": "FREQ=DAILY;UNTIL=20200101T000000Z"}, BNE, "no future runs"),
        ("once", {"at": "2020-01-01T09:00"}, BNE, "no future runs"),
        ("hourly-ish", {}, BNE, "Unknown schedule type"),
    ],
)
def test_bad_schedules_are_refused(kind: str, fields: dict, tz: str, message: str) -> None:
    with pytest.raises(ScheduleError, match=message):
        schedule_service.build(kind, fields, tz)


# -- the form ---------------------------------------------------------------------

BUSINESS_HOURS = {
    "name": "business hours",
    "kind": "every",
    "timezone": BNE,
    "every_n": "15",
    "every_unit": "minutes",
    "days": ["MO", "TU", "WE", "TH", "FR"],
    "window_start": "06:00",
    "window_end": "20:00",
    "enabled_present": "1",
    "enabled": "1",
}


def _schedules(strata: Strata, dm_uuid: str):
    with strata.app.app_context():
        from strata.models.schedule import Schedule

        return Schedule.for_datamodule(strata.datamodule(dm_uuid).id)


def test_create_a_schedule_and_see_it_listed(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(f"/datamodules/{dm_uuid}/schedules/new", data=BUSINESS_HOURS)
    assert resp.status_code == 302
    [schedule] = _schedules(strata, dm_uuid)
    assert schedule.enabled and schedule.next_run_at is not None
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert (
        "business hours" in page
        and "Every 15 minutes on Mon, Tue, Wed, Thu, Fri, 06:00-20:00" in page
    )
    assert "AEST" in page  # next run shown in the schedule's own timezone


def test_an_invalid_form_rerenders_with_the_error(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    bad = {**BUSINESS_HOURS, "window_start": "06:30"}
    resp = strata.client.post(f"/datamodules/{dm_uuid}/schedules/new", data=bad)
    assert resp.status_code == 200 and "on the hour" in resp.get_data(as_text=True)
    assert _schedules(strata, dm_uuid) == []


def test_preview_shows_the_next_runs_or_the_problem(strata: Strata) -> None:
    ok = strata.client.post("/datamodules/schedules/preview", data=BUSINESS_HOURS)
    body = ok.get_data(as_text=True)
    assert "RRULE:FREQ=MINUTELY;INTERVAL=15" in body and body.count("<li>") == 5
    bad = strata.client.post(
        "/datamodules/schedules/preview", data={**BUSINESS_HOURS, "every_n": "x"}
    )
    assert "whole number" in bad.get_data(as_text=True)


def test_edit_toggle_and_delete(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.client.post(f"/datamodules/{dm_uuid}/schedules/new", data=BUSINESS_HOURS)
    [schedule] = _schedules(strata, dm_uuid)

    form = strata.client.get(f"/datamodules/schedules/{schedule.uuid}/edit").get_data(as_text=True)
    assert 'value="06:00"' in form and "checked" in form

    weekly = {
        "name": "fortnightly",
        "kind": "weekly",
        "timezone": "Pacific/Auckland",
        "every_weeks": "2",
        "days": ["MO"],
        "time": "07:00",
        "start_date": "2026-11-23",
        "enabled_present": "1",
        "enabled": "1",
    }
    resp = strata.client.post(f"/datamodules/schedules/{schedule.uuid}/edit", data=weekly)
    assert resp.status_code == 302
    [schedule] = _schedules(strata, dm_uuid)
    assert schedule.name == "fortnightly" and "INTERVAL=2" in schedule.schedule_definition

    strata.client.post(f"/datamodules/schedules/{schedule.uuid}/toggle")
    [schedule] = _schedules(strata, dm_uuid)
    assert not schedule.enabled and schedule.next_run_at is None
    strata.client.post(f"/datamodules/schedules/{schedule.uuid}/toggle")
    [schedule] = _schedules(strata, dm_uuid)
    assert schedule.enabled and schedule.next_run_at is not None

    strata.client.post(f"/datamodules/schedules/{schedule.uuid}/delete")
    assert _schedules(strata, dm_uuid) == []


def test_an_unticked_enabled_box_saves_it_disabled(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    form = {k: v for k, v in BUSINESS_HOURS.items() if k != "enabled"}
    strata.client.post(f"/datamodules/{dm_uuid}/schedules/new", data=form)
    [schedule] = _schedules(strata, dm_uuid)
    assert not schedule.enabled and schedule.next_run_at is None


def test_timezones_come_from_the_setting(strata: Strata) -> None:
    strata.set_config({"schedule.timezones": "Europe/London, Bogus/Zone, Asia/Tokyo"})
    dm_uuid = strata.new_datamodule()
    page = strata.client.get(f"/datamodules/{dm_uuid}/schedules/new").get_data(as_text=True)
    assert "Europe/London" in page and "Asia/Tokyo" in page and "Bogus/Zone" not in page
    assert '<option value="Europe/London" selected>' in page  # the first is the default


def test_schedules_need_an_admin(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/schedules/new", data=BUSINESS_HOURS, headers={"X-User": "viewer"}
    )
    assert resp.status_code == 302 and _schedules(strata, dm_uuid) == []


# -- the worker's tick -------------------------------------------------------------


EVERY_15 = {
    "name": "every 15",
    "kind": "every",
    "timezone": BNE,
    "every_n": "15",
    "every_unit": "minutes",
    "enabled_present": "1",
    "enabled": "1",
}


def _due(
    strata: Strata, dm_uuid: str, overdue: timedelta, enabled: bool = True, form: dict | None = None
) -> None:
    """Create a schedule (business hours by default) and mark it overdue."""
    form = {**(form or BUSINESS_HOURS)}
    if not enabled:
        form.pop("enabled")
    strata.client.post(f"/datamodules/{dm_uuid}/schedules/new", data=form)
    with strata.app.app_context():
        from strata.db import transaction

        with transaction() as cur:
            cur.execute(
                "UPDATE schedule SET next_run_at = ?",
                ((datetime.now(UTC) - overdue).isoformat(),),
            )


def _jobs(strata: Strata, dm_uuid: str):
    with strata.app.app_context():
        from strata.models.build_job import BuildJob

        return BuildJob.recent_for_datamodule(strata.datamodule(dm_uuid).id)


def _tick(strata: Strata) -> list[str]:
    with strata.app.app_context():
        return schedule_service.run_due()


@pytest.fixture
def queued(strata: Strata) -> Strata:
    strata.app.config["WORKER_INLINE"] = False
    return strata


def test_a_due_schedule_queues_a_forced_build_and_moves_on(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    _due(queued, dm_uuid, timedelta(minutes=1))
    assert _tick(queued) == ["test/sales: business hours"]
    [job] = _jobs(queued, dm_uuid)
    assert job.reason == "schedule:business hours" and job.force and job.status == "queued"
    [schedule] = _schedules(queued, dm_uuid)
    assert schedule.last_run_at is not None
    assert datetime.fromisoformat(schedule.next_run_at or "") > datetime.now(UTC)
    assert _tick(queued) == []  # not due again yet


def test_missed_runs_collapse_into_one(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    # Every day, so the outcome does not depend on what weekday the tests run.
    _due(queued, dm_uuid, timedelta(days=3), form=EVERY_15)  # ~290 runs missed
    assert len(_tick(queued)) == 1
    [schedule] = _schedules(queued, dm_uuid)
    assert datetime.fromisoformat(schedule.next_run_at or "") > datetime.now(UTC)
    assert _tick(queued) == []  # no catch-up storm
    assert len(_jobs(queued, dm_uuid)) == 1


def test_a_disabled_schedule_does_not_run(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    _due(queued, dm_uuid, timedelta(minutes=1), enabled=False)
    assert _tick(queued) == [] and _jobs(queued, dm_uuid) == []


def test_two_workers_ticking_together_run_it_once(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    _due(queued, dm_uuid, timedelta(minutes=1))
    with queued.app.app_context():
        from strata.models.schedule import Schedule

        [a] = Schedule.for_datamodule(queued.datamodule(dm_uuid).id)
        [b] = Schedule.for_datamodule(queued.datamodule(dm_uuid).id)
        now = datetime.now(UTC).isoformat()
        assert a.claim_run(now, None) is True
        assert b.claim_run(now, None) is False  # b's snapshot of next_run_at is stale


def test_an_invalid_stored_definition_disables_the_schedule(queued: Strata) -> None:
    dm_uuid = queued.new_datamodule()
    _due(queued, dm_uuid, timedelta(minutes=1))
    with queued.app.app_context():
        from strata.db import transaction

        with transaction() as cur:
            cur.execute('UPDATE schedule SET schedule_definition = \'{"kind": "every"}\'')
    assert _tick(queued) == []
    [schedule] = _schedules(queued, dm_uuid)
    assert not schedule.enabled


def test_worker_once_ticks_then_builds(queued: Strata) -> None:
    from strata.worker import Worker

    dm_uuid = queued.new_datamodule()
    _due(queued, dm_uuid, timedelta(minutes=1))
    Worker(queued.app, ["default"], concurrency=1).run(once=True)
    [job] = _jobs(queued, dm_uuid)
    assert job.status == "done"
    assert queued.datamodule(dm_uuid).build_status == "ready"
