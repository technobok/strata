"""The Format page (blueprints/formatting.py): a report's settings, checked as they are
typed (the preview endpoint), refused with every problem marked when saved wrong,
previewed on the report's first page. Owners only (tests/test_access_lists.py sweeps
its routes)."""

import json
import re
from typing import Any

from tests.conftest import Strata, as_user
from tests.test_formatting_render import SCRIPT, _setup
from tests.test_presentation import _import, _report

COLUMNS = ["Company", "Item", "ItemURL", "Amount", "Sold on", "Path", "Evil", "Nav"]


def _form(**changes: dict[str, str]) -> dict[str, str]:
    """The page's form as a browser posts it: every column (no settings), then
    ``changes`` per column name, e.g. Amount={"format": "0.00"}."""
    data: dict[str, str] = {}
    for n, name in enumerate(COLUMNS):
        data[f"c-{n}-name"] = name
        data[f"c-{n}-align"] = "auto"
        for key, value in changes.get(name, {}).items():
            data[f"c-{n}-{key}"] = value
    return data


def _url(rpt: str, end: str = "") -> str:
    return f"/datamodules/reports/{rpt}/format{end}"


def _stored(strata: Strata) -> Any:
    from strata.services.presentation import from_json

    return from_json(_report(strata, "Lines", "test/lines").presentation)


def test_the_page_lists_the_columns_with_their_settings_and_samples(strata: Strata) -> None:
    rpt = _setup(strata)
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    for name in COLUMNS:
        assert f'value="{name}"' in page
    assert "<code>DECIMAL(18,2)</code>" in page
    assert 'name="c-3-format" value="#,##0.00;(#,##0.00)"' in page  # Amount's code
    assert (
        '<output data-sample="c-3-format" title="Its first value with this format">1,234.50</output>'
        in page
    )
    assert 'title="Its first value, plain">1234.50</small>' in page
    assert re.search(r'name="c-2-hidden" checked', page)  # ItemURL
    assert "First row: /nav?item=A%201%2F2" in page  # Item's link
    # the preview: the first page with the settings
    assert '<td class="align-center">1,234.50</td>' in page


def test_the_format_box_offers_presets_then_codes_other_reports_use(strata: Strata) -> None:
    rpt = _setup(strata)
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    codes = re.search(r'<datalist id="c-3-codes">(.*?)</datalist>', page)
    assert codes is not None
    offered = re.findall(r'<option value="([^"]*)">', codes.group(1))
    assert offered[:2] == ["#,##0.00", "#,##0.00;[Red]-#,##0.00"]
    assert "#,##0.00;(#,##0.00)" in offered  # used by a report (this one)
    assert "d mmm yyyy" not in offered  # a date code does not fit a number
    dates = re.search(r'<datalist id="c-4-codes">(.*?)</datalist>', page)
    assert dates is not None and "d mmm yyyy" in dates.group(1)


def test_without_live_data_the_page_says_to_build(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(path="test/lines", script=SCRIPT)
    rpt = strata.new_report(dm_uuid, body="SELECT * FROM lines", name="Lines")
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    assert "Build the datamodule to see the report's columns" in page
    assert 'id="format-form"' not in page


def test_saving_sets_the_columns_and_keeps_the_rest(strata: Strata) -> None:
    rpt = _setup(strata)
    _import(  # a style rule from a definition file: this page leaves it alone
        strata,
        'path = "test/lines"\n[[report]]\nname = "Lines"\nbody = "SELECT * FROM lines"\n'
        '[[report.style]]\nwhen = \'"Amount" < 0\'\ncolumns = []\nstyle = "red"\n',
    )
    resp = strata.client.post(
        _url(rpt), data=_form(Amount={"format": "0.0", "align": "right"}, Path={"hidden": "on"})
    )
    assert resp.status_code == 302 and resp.headers["Location"].endswith(_url(rpt))
    stored = _stored(strata)
    assert stored.setting("Amount").format == "0.0" and stored.setting("Amount").align == "right"
    assert stored.setting("Path").hidden
    assert stored.setting("Item").link == ""  # this post gave Item no link
    assert [r.style for r in stored.styles] == ["red"]  # kept
    page = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    assert '<td class="align-right">1234.5</td>' in page


def test_a_refused_save_marks_each_problem_and_keeps_what_was_typed(strata: Strata) -> None:
    rpt = _setup(strata)
    before = _report(strata, "Lines", "test/lines").presentation
    resp = strata.client.post(
        _url(rpt),
        data=_form(Amount={"format": "yyyy-mm-dd"}, Item={"link": "/nav?item={Itme}"}),
    )
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "Not saved: the report&#39;s formatting has problems." in page
    assert re.search(r'name="c-3-format" value="yyyy-mm-dd"[^>]*aria-invalid="true"', page, re.S)
    assert (
        '<small class="field-message" data-message-for="column:Amount:format">Amount: format: '
        "This column holds numbers" in page
    )
    assert (
        "Item: link: &#39;Itme&#39; is not a column of this report (did you mean &#39;Item&#39;?)"
        in page
    )
    assert _report(strata, "Lines", "test/lines").presentation == before  # nothing saved


def test_the_preview_checks_and_shows_a_draft_without_saving(strata: Strata) -> None:
    rpt = _setup(strata)
    before = _report(strata, "Lines", "test/lines").presentation
    resp = strata.client.post(
        _url(rpt, "/preview"),
        data=_form(Amount={"format": "#,##0.000"}, Company={"format": "0.00"}),
    )
    assert resp.status_code == 200
    data = json.loads(resp.get_data(as_text=True))
    assert data["problems"] == {
        "column:Company:format": 'Company: format: A text column takes @ (e.g. "Item "@) '
        "(at character 1)"
    }
    assert data["summary"] == list(data["problems"].values())
    assert data["samples"]["c-3"] == {"format": "1,234.500", "link": ""}
    assert '<td class="align-right">1,234.500</td>' in data["preview"]
    assert _report(strata, "Lines", "test/lines").presentation == before


def test_settings_of_a_column_gone_are_listed_until_removed(strata: Strata) -> None:
    rpt = _setup(strata)
    resp = strata.client.post(
        f"/datamodules/reports/{rpt}/edit",
        data={"name": "Lines", "body_sql": 'SELECT * EXCLUDE ("Sold on") FROM lines'},
    )
    assert resp.status_code == 302
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    assert (
        "No longer in the result" in page
        and "Remove the settings of <strong>Sold on</strong>" in page
    )
    stale = re.search(r'name="s-0-json" value="([^"]*)"', page)
    assert stale is not None
    kept = stale.group(1).replace("&#34;", '"')
    assert json.loads(kept) == {"name": "Sold on", "format": "d mmm yyyy"}

    form = {
        k: v
        for k, v in _form(Amount={"format": "0"}).items()
        if not k.startswith("c-4-")  # "Sold on" is not on the page any more
    }
    # renumber what follows it, as the page does
    renumbered: dict[str, str] = {}
    names = [n for n in COLUMNS if n != "Sold on"]
    for n, name in enumerate(names):
        old = COLUMNS.index(name)
        for key in ("name", "align", "format"):
            if f"c-{old}-{key}" in form:
                renumbered[f"c-{n}-{key}"] = form[f"c-{old}-{key}"]
    renumbered["s-0-json"] = kept
    refused = strata.client.post(_url(rpt), data=renumbered).get_data(as_text=True)
    assert "&#39;Sold on&#39; is not a column of this report" in refused
    assert strata.client.post(_url(rpt), data=renumbered | {"s-0-remove": "on"}).status_code == 302
    assert "Sold on" not in [c.name for c in _stored(strata).columns]


def test_a_required_argument_without_a_default_is_asked_for(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(path="test/lines", script=SCRIPT)
    strata.build(dm_uuid)
    rpt = strata.new_report(
        dm_uuid,
        body='SELECT * FROM lines WHERE "Company" = $co',
        name="Lines",
        args=[{"name": "co", "type": "text", "widget": "input", "required": "yes"}],
    )
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    assert "The preview needs a value for <code>co</code>" in page
    assert 'name="arg-co"' in page
    resp = strata.client.post(_url(rpt, "/preview"), data=_form() | {"arg-co": "NZ"})
    preview = json.loads(resp.get_data(as_text=True))["preview"]
    assert "The first 1 of 1 row" in preview and "<td>NZ</td>" in preview


def test_only_owners_see_the_format_button(strata: Strata) -> None:
    rpt = _setup(strata)
    mine = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    assert f'href="{_url(rpt)}"' in mine
    theirs = strata.client.get(f"/r/{rpt}.html", headers=as_user("viewer")).get_data(as_text=True)
    assert f'href="{_url(rpt)}"' not in theirs
    assert strata.client.get(_url(rpt), headers=as_user("viewer")).status_code == 403


def test_each_view_and_preview_is_a_read(strata: Strata) -> None:
    rpt = _setup(strata)
    strata.client.get(_url(rpt))
    strata.client.post(_url(rpt, "/preview"), data=_form())
    with strata.app.app_context():
        from strata.db import get_db

        rows = (
            get_db()
            .execute("SELECT kind, report_name, status FROM read_log ORDER BY id")
            .fetchall()
        )
    assert [tuple(r) for r in rows][-2:] == [("preview", "Lines", 200), ("preview", "Lines", 200)]


def test_a_locked_datamodule_shows_no_data(strata: Strata) -> None:
    rpt = _setup(strata)
    with strata.app.app_context():
        from strata.db import transaction
        from strata.models.datamodule import Datamodule

        dm = Datamodule.get_by_path("test/lines")
        assert dm is not None
        with transaction() as cur:
            cur.execute(
                "UPDATE datamodule SET access_lock = 'x may not read y' WHERE id = ?", (dm.id,)
            )
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    assert "test/lines is locked: x may not read y" in page
    assert "1234.50" not in page and 'id="format-form"' not in page
    data = json.loads(
        strata.client.post(_url(rpt, "/preview"), data=_form()).get_data(as_text=True)
    )
    assert data == {"problems": {}, "summary": [], "samples": {}, "preview": ""}
