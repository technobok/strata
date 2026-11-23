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
    rpt = strata.new_report(dm_uuid, body=NEGATIVE, name="Negative", macro="neg", check="error")
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
