"""A report must run on its datamodule's data: saving one that does not is refused
(what was typed stays, the error stays on screen), and a build that breaks a report
that ran records it, shows it on the datamodule page and mails the alert list once.
A refused read keeps its own text in the read audit (Excel does not show it).
"""

import html
from typing import Any

import pytest

from strata.models.datamodule import Datamodule
from tests.conftest import SALES_SCRIPT, Strata

GOOD = "SELECT region, revenue FROM sales"
MISSING_TABLE = "SELECT * FROM no_such_table"


def _post_report(strata: Strata, dm_uuid: str, body: str, **form: Any) -> Any:
    data: dict[str, Any] = {"name": "Sales", "macro_name": "sales_report", "body_sql": body}
    data.update(form)
    return strata.client.post(f"/datamodules/{dm_uuid}/reports/new", data=data)


def _reports(strata: Strata, dm_uuid: str) -> list[Any]:
    with strata.app.app_context():
        from strata.models.report import Report

        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        return Report.get_for_datamodule(dm.id)


def _built(strata: Strata, script: str = SALES_SCRIPT) -> str:
    dm_uuid = strata.new_datamodule(script=script)
    strata.build(dm_uuid)
    return dm_uuid


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_a_report_on_a_missing_table_is_not_saved_and_the_form_keeps_it(
    strata: Strata,
) -> None:
    dm_uuid = _built(strata)
    resp = _post_report(
        strata,
        dm_uuid,
        MISSING_TABLE,
        arg_name=["region"],
        arg_type=["text"],
        arg_widget=["input"],
        arg_required=["no"],
        arg_default=["North"],
        arg_constraint=[""],
        arg_description=["which region"],
        is_check="on",
        severity="error",
    )
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert '<div class="error-box report-error">Not saved: the report does not run' in page
    assert "no_such_table" in page and "does not exist" in page
    # Everything typed is still in the form.
    assert 'name="name" value="Sales"' in page and 'value="sales_report"' in page
    assert f">{MISSING_TABLE}</textarea>" in page
    assert 'name="arg_name" value="region"' in page and 'value="which region"' in page
    assert 'name="is_check" checked' in page and '<option value="error" selected>' in page
    assert _reports(strata, dm_uuid) == []
    assert strata.flashes() == []  # not a flash that fades after 5 seconds


def test_a_refused_edit_keeps_what_was_typed_and_the_saved_report(strata: Strata) -> None:
    dm_uuid = _built(strata)
    rpt = strata.new_report(dm_uuid, body=GOOD)
    resp = strata.client.post(
        f"/datamodules/reports/{rpt}/edit",
        data={
            "name": "Renamed",
            "macro_name": "sales_report",
            "body_sql": "SELECT nope FROM sales",
        },
    )
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Not saved" in page and "nope" in page
    assert 'name="name" value="Renamed"' in page
    (saved,) = _reports(strata, dm_uuid)
    assert saved.name == "Sales" and saved.body_sql == GOOD


@pytest.mark.parametrize(
    ("form", "words"),
    [
        ({"body_sql": "SELECT missing_column FROM sales"}, "missing_column"),
        (
            {
                "body_sql": "SELECT * FROM sales WHERE revenue > $n",
                "arg_name": ["n"],
                "arg_type": ["integer"],
                "arg_widget": ["input"],
                "arg_required": ["no"],
                "arg_default": ["lots"],
                "arg_constraint": [""],
                "arg_description": [""],
            },
            "Argument 'n'",
        ),
        (
            {
                "body_sql": "SELECT * FROM sales WHERE region = $region",
                "arg_name": ["region"],
                "arg_type": ["text"],
                "arg_widget": ["select"],
                "arg_required": ["no"],
                "arg_default": [""],
                "arg_constraint": ["SELECT DISTINCT area FROM sales"],
                "arg_description": [""],
            },
            "The constraint query of 'region'",
        ),
    ],
)
def test_what_a_read_would_hit_first_refuses_the_save(
    strata: Strata, form: dict[str, Any], words: str
) -> None:
    dm_uuid = _built(strata)
    body = form.pop("body_sql")
    page = html.unescape(_post_report(strata, dm_uuid, body, **form).get_data(as_text=True))
    assert "Not saved: the report does not run" in page and words in page
    assert _reports(strata, dm_uuid) == []


def test_a_required_argument_without_a_default_binds_as_its_type(strata: Strata) -> None:
    """date_trunc() has several overloads: an untyped NULL would not bind, a DATE does."""
    dm_uuid = _built(strata)
    resp = _post_report(
        strata,
        dm_uuid,
        "SELECT * FROM sales WHERE sold_on >= date_trunc('month', $since)",
        arg_name=["since"],
        arg_type=["date"],
        arg_widget=["input"],
        arg_required=["yes"],
        arg_default=[""],
        arg_constraint=[""],
        arg_description=[""],
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)


def test_a_datamodule_with_no_data_yet_saves_any_report(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()  # never built: nothing to check against
    assert _post_report(strata, dm_uuid, MISSING_TABLE).status_code == 302


# ---------------------------------------------------------------------------
# After a build
# ---------------------------------------------------------------------------


def _scheduled_build(strata: Strata, dm_uuid: str) -> None:
    from strata.services import build_queue

    with strata.app.app_context():
        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        build_queue.request_build(dm, "schedule:nightly", force=True)


def test_a_build_that_breaks_a_report_records_shows_and_mails_it_once(
    strata: Strata, mail: list[Any]
) -> None:
    dm_uuid = _built(strata)
    rpt = strata.new_report(dm_uuid, body=GOOD)
    strata.set_script(dm_uuid, "CREATE TABLE other AS SELECT 1 AS x")
    _scheduled_build(strata, dm_uuid)

    (report,) = _reports(strata, dm_uuid)
    assert report.problem is not None and "sales" in report.problem
    broken = [m for m in mail if m.subject == "[STRATA] test/sales: 1 report broken"]
    assert len(broken) == 1
    assert "<strong>Sales</strong>" in broken[0].html and "sales" in broken[0].html

    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert 'class="report-broken"' in page and "does not run on this data" in page
    edit = strata.client.get(f"/datamodules/reports/{rpt}/edit").get_data(as_text=True)
    assert '<div class="error-box report-problem">' in edit

    _scheduled_build(strata, dm_uuid)  # still broken: no second mail
    assert len([m for m in mail if "report broken" in m.subject]) == 1

    strata.set_script(dm_uuid, SALES_SCRIPT)
    _scheduled_build(strata, dm_uuid)  # the table is back: the problem clears
    (report,) = _reports(strata, dm_uuid)
    assert report.problem is None
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert 'class="report-broken"' not in page


def test_the_build_button_records_but_does_not_mail(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = _built(strata)
    strata.new_report(dm_uuid, body=GOOD)
    strata.set_script(dm_uuid, "CREATE TABLE other AS SELECT 1 AS x")
    strata.build(dm_uuid)
    (report,) = _reports(strata, dm_uuid)
    assert report.problem is not None
    assert not [m for m in mail if "broken" in m.subject]


def test_a_restricted_datamodules_broken_report_mail_quotes_no_error(
    strata: Strata, mail: list[Any]
) -> None:
    from strata.services import notify_service

    dm_uuid = _built(strata)
    strata.new_report(dm_uuid, body=GOOD)
    assert strata.set_access(dm_uuid, owners=None, viewers="@brs-finance").status_code == 302
    with strata.app.app_context():
        restricted = Datamodule.get_by_uuid(dm_uuid)
        assert restricted is not None and not notify_service.quotes_errors(restricted)
    strata.set_script(dm_uuid, "CREATE TABLE other AS SELECT 1 AS x")
    _scheduled_build(strata, dm_uuid)
    (sent,) = [m for m in mail if "report broken" in m.subject]
    assert "<strong>Sales</strong>" in sent.html
    assert "does not exist" not in sent.html and "Catalog Error" not in sent.html


# ---------------------------------------------------------------------------
# The read audit keeps a refusal's text
# ---------------------------------------------------------------------------


def _details(strata: Strata) -> list[tuple[int, str | None]]:
    with strata.app.app_context():
        from strata.db import get_db

        rows = get_db().execute("SELECT status, detail FROM read_log ORDER BY id").fetchall()
    return [(int(s), d) for s, d in rows]


def test_a_refused_read_keeps_its_reason(strata: Strata) -> None:
    dm_uuid = _built(strata)
    rpt = strata.new_report(dm_uuid, body=GOOD)
    strata.client.get(f"/r/{rpt}.csv?region__zz=1")  # an unknown filter operator: 400
    strata.client.get(f"/r/{rpt}.csv")
    (refused, ok) = _details(strata)
    assert refused[0] == 400 and refused[1] and "zz" in refused[1]
    assert ok == (200, None)


def test_an_html_error_page_is_not_kept_as_a_reason(strata: Strata) -> None:
    dm_uuid = _built(strata)
    rpt = strata.new_report(dm_uuid, body=GOOD)
    assert strata.set_access(dm_uuid, owners=None, viewers="jo").status_code == 302
    resp = strata.client.get(f"/r/{rpt}.html", headers={"X-User": "outsider"})
    assert resp.status_code == 403 and resp.get_data(as_text=True).lstrip().startswith("<")
    assert _details(strata)[-1] == (403, None)
    # werkzeug's own error pages (an unknown ?version= is a 404) are streamed: not kept.
    unknown = strata.client.get(f"/r/{rpt}.csv?version=00000000-0000-0000-0000-000000000000")
    assert unknown.status_code == 404
    assert _details(strata)[-1] == (404, None)
    strata.client.get(f"/r/{rpt}.csv", headers={"X-User": "outsider"})  # a text refusal
    status, detail = _details(strata)[-1]
    assert status == 403 and detail and "viewers" in detail


@pytest.mark.parametrize(
    ("body", "status", "kept"),
    [
        ("unknown filter operator 'zz'", 400, "unknown filter operator 'zz'"),
        ("  invalid API key\n", 403, "invalid API key"),
        ("<p>short HTML page</p>", 403, None),
        ("x" * 2001, 400, None),
        ("fine", 200, None),
        ("", 400, None),
    ],
)
def test_which_refusal_texts_are_kept(body: str, status: int, kept: str | None) -> None:
    from flask import Response

    from strata.services import read_audit

    assert read_audit._refusal_text(Response(body, status=status)) == kept


def test_a_streamed_refusal_is_not_read(app: Any) -> None:
    from flask import Response

    from strata.services import read_audit

    with app.test_request_context():
        resp = Response(iter([b"streamed"]), status=400)
        assert resp.is_streamed and read_audit._refusal_text(resp) is None
