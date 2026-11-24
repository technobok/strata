"""Mail from a restricted datamodule, and recipients outside its viewers (AC6).

A datamodule whose viewers are not everyone (*) mails its runs as the count, the
data-as-of line and a link: no rows (mail bodies sit in Outbox's shared database).
Mail still goes to every recipient; the schedule form and the report's schedule
list name the ones who cannot open the run.
"""

from typing import Any

from tests.conftest import Strata
from tests.test_notify import BAD_ROW_SCRIPT, NEGATIVE, _fire, _schedule


def _check(strata: Strata, viewers: str | None) -> tuple[str, str]:
    dm_uuid = strata.new_datamodule(script=BAD_ROW_SCRIPT)
    rpt = strata.new_report(dm_uuid, body=NEGATIVE, name="Negative", check="error")
    strata.build(dm_uuid)
    assert strata.set_access(dm_uuid, owners=None, viewers=viewers).status_code == 302
    return dm_uuid, rpt


def test_a_restricted_datamodule_mails_no_rows(strata: Strata, mail: list[Any]) -> None:
    _dm, rpt = _check(strata, viewers="@brs-finance")
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _fire(strata)
    [sent] = mail
    assert sent.subject == "[ERROR] Negative — 1 row"
    assert "1 row. Who may read test/sales is restricted, so the rows are not in this mail" in (
        sent.html
    )
    assert "South" not in sent.html and "<table" not in sent.html
    assert "/runs/" in sent.html  # the link to the run


def test_an_open_datamodule_mails_its_first_rows(strata: Strata, mail: list[Any]) -> None:
    _dm, rpt = _check(strata, viewers=None)  # the default: everyone
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [sent] = mail
    assert "South" in sent.html and "<table" in sent.html and "restricted" not in sent.html


def test_recipients_outside_the_viewers_are_named(strata: Strata, mail: list[Any]) -> None:
    mail_gk = strata.app.config["GATEKEEPER_CLIENT"]
    mail_gk.groups["sales"] = ["root", "dora"]
    mail_gk.users["dora"] = mail_gk.users["alice"].__class__("dora", "dora@example.com")
    _dm, rpt = _check(strata, viewers="alice")
    resp = _schedule(strata, rpt, "alice\nroot\njsmith\n@sales\nops@example.com")
    assert resp.status_code == 302
    expected = (
        "not viewers of test/sales: jsmith, @sales (dora); "
        "email addresses Strata cannot match to a viewer: ops@example.com"
    )
    assert (
        "error",
        f"Mail goes to them anyway, but some cannot open the run: {expected}.",
    ) in strata.flashes()
    page = strata.client.get(f"/datamodules/reports/{rpt}/edit").get_data(as_text=True)
    assert f"Cannot open the run: {expected}" in page
    with strata.app.app_context():
        from strata.models.report import Report
        from strata.models.schedule import Schedule

        report = Report.get_by_uuid(rpt)
        assert report is not None
        [schedule] = Schedule.for_report(report.id)
    form = strata.client.get(f"/datamodules/schedules/{schedule.uuid}/edit").get_data(as_text=True)
    assert f"some cannot open the run it links to: {expected}." in form
    assert "so its mail carries the count and a link to the run, not the rows" in form


def test_everyone_may_open_an_open_datamodules_runs(strata: Strata, mail: list[Any]) -> None:
    _dm, rpt = _check(strata, viewers=None)
    _schedule(strata, rpt, "jsmith\nops@example.com")
    assert not any("cannot open" in msg for _cat, msg in strata.flashes())
    page = strata.client.get(f"/datamodules/reports/{rpt}/edit").get_data(as_text=True)
    assert "Cannot open the run" not in page


def test_recipients_that_cannot_be_checked_say_so(strata: Strata, mail: list[Any]) -> None:
    _dm, rpt = _check(strata, viewers="alice")
    strata.app.config["GATEKEEPER_CLIENT"] = None
    _schedule(strata, rpt, "jsmith")
    assert any(
        "the recipients could not be checked against its viewers" in msg
        for _cat, msg in strata.flashes()
    )


# ---------------------------------------------------------------------------
# Error texts: a failed gate check's error lists its first rows, and DuckDB quotes
# the values it cannot convert, so mail about a restricted datamodule quotes no
# error either (it says the error is in Strata).
# ---------------------------------------------------------------------------

NOT_QUOTED = "not quoted here, as who may read test/sales is restricted: open Strata to read it"
GATE_SAMPLE = "South, B, 250.5"  # the row the gate below finds


def _failing_gate(strata: Strata, viewers: str | None, ttl: str = "") -> str:
    """A datamodule built once, then given a gate check that finds a row: its next
    automatic build fails with that row in the error."""
    dm_uuid = strata.new_datamodule(ttl=ttl)
    strata.build(dm_uuid)
    assert strata.set_access(dm_uuid, owners=None, viewers=viewers).status_code == 302
    strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE revenue > 200",
        name="Big",
        check="error",
        gate=True,
    )
    return dm_uuid


def _scheduled_build_and_stale_sweep(strata: Strata, dm_uuid: str) -> None:
    from tests.test_alerts import _age_live_version, _alerts_form, _scheduled_build, _sweep

    assert _alerts_form(strata, dm_uuid, "", "1").status_code == 302  # stale after 1 hour
    _scheduled_build(strata, dm_uuid)  # fails on the gate: "build failing"
    _age_live_version(strata, dm_uuid, 2)
    assert _sweep(strata) == ["test/sales"]  # "data is stale", with the last build error


def test_restricted_alerts_do_not_quote_the_build_error(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = _failing_gate(strata, viewers="@brs-finance")
    _scheduled_build_and_stale_sweep(strata, dm_uuid)
    failing, stale = mail
    assert failing.subject == "[STRATA] test/sales: build failing"
    assert stale.subject == "[STRATA] test/sales: data is stale"
    for sent in (failing, stale):
        assert GATE_SAMPLE not in sent.html and "found 1 row" not in sent.html
    assert f"Error: {NOT_QUOTED}." in failing.html
    assert f"Last build error: {NOT_QUOTED}." in stale.html
    # Strata itself still shows the error to the datamodule's owners.
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert GATE_SAMPLE in page


def test_open_alerts_quote_the_build_error(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = _failing_gate(strata, viewers=None)
    _scheduled_build_and_stale_sweep(strata, dm_uuid)
    failing, stale = mail
    assert GATE_SAMPLE in failing.html and GATE_SAMPLE in stale.html
    assert "not quoted here" not in failing.html + stale.html


def test_a_restricted_run_on_old_data_says_the_refresh_failed_without_why(
    strata: Strata, mail: list[Any]
) -> None:
    dm_uuid = _failing_gate(strata, viewers="@brs-finance", ttl="0")  # always stale
    rpt = strata.new_report(dm_uuid, name="All")
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _fire(strata)  # the run refreshes first: the gate fails it, the run uses the old data
    alert, sent = mail
    assert alert.subject == "[STRATA] test/sales: build failing"
    first_line = sent.html.split("</p>")[0]
    assert f"refresh failed: {NOT_QUOTED}" in first_line
    assert GATE_SAMPLE not in alert.html + sent.html


def test_a_restricted_failed_run_does_not_quote_its_error(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(
        dm_uuid, body="SELECT CAST(region AS INTEGER) AS n FROM sales", name="Cast"
    )
    strata.build(dm_uuid)
    assert strata.set_access(dm_uuid, owners=None, viewers="@brs-finance").status_code == 302
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [sent] = mail
    assert sent.subject == "[FAILED] Cast — the scheduled run failed"
    assert "North" not in sent.html and f"Error: {NOT_QUOTED}." in sent.html


def test_an_open_failed_run_quotes_its_error(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(
        dm_uuid, body="SELECT CAST(region AS INTEGER) AS n FROM sales", name="Cast"
    )
    strata.build(dm_uuid)
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [sent] = mail
    assert "North" in sent.html and "not quoted here" not in sent.html
