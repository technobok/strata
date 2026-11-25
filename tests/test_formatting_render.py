"""A report's settings as they are shown (step 4 of the presentation arc): formats,
alignment, hidden columns and links on the page and run page; formats and hidden
columns in mail (never links); number formats and alignment in xlsx downloads; raw
values in the other downloads. Search skips hidden columns everywhere.
"""

import io
import json
import re
from typing import Any

import pytest
from openpyxl import load_workbook

from tests.conftest import Strata
from tests.test_checks import _run_now
from tests.test_notify import _fire, _schedule
from tests.test_presentation import _import, _report

SCRIPT = """
CREATE TABLE lines AS SELECT * FROM (VALUES
  ('AU', 'A 1/2', 'https://shop.example/a', 1234.5::DECIMAL(18,2), DATE '2026-11-05',
   '/nav?item=A', 'javascript:alert(1)', 'navision://client/item/A'),
  ('NZ', 'B&C', NULL, -5::DECIMAL(18,2), DATE '2026-11-06',
   NULL, 'java' || chr(9) || 'script:alert(2)', NULL)
) AS t("Company", "Item", "ItemURL", "Amount", "Sold on", "Path", "Evil", "Nav");
"""

LOOK = """
path = "test/lines"

[[report]]
name = "Lines"
body = "SELECT * FROM lines"

[[report.column]]
name = "Amount"
format = "#,##0.00;(#,##0.00)"
align = "center"

[[report.column]]
name = "Sold on"
format = "d mmm yyyy"

[[report.column]]
name = "Item"
link = "/nav?item={Item}"
link_text = "Open {Item}"
new_tab = true

[[report.column]]
name = "ItemURL"
hidden = true

[[report.column]]
name = "Company"
link = "{+ItemURL}"

[[report.column]]
name = "Evil"
link = "{+Evil}"

[[report.column]]
name = "Path"
link = "r/x.html?p={Path}"

[[report.column]]
name = "Nav"
link = "{+Nav}"
"""


def _setup(strata: Strata) -> str:
    dm_uuid = strata.new_datamodule(path="test/lines", script=SCRIPT)
    strata.build(dm_uuid)
    out = _import(strata, LOOK)
    assert out.refused is None, out.refused
    return _report(strata, "Lines", "test/lines").uuid


def _page(strata: Strata, url: str) -> str:
    resp = strata.client.get(url)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_data(as_text=True)


def _headers(page: str) -> list[str]:
    return re.findall(r'<th class="[^"]*"><a\s[^>]*>([^<]+)</a></th>', page)


def test_formats_alignment_and_hidden_columns_on_the_page(strata: Strata) -> None:
    rpt = _setup(strata)
    page = _page(strata, f"/r/{rpt}.html")
    assert _headers(page) == ["Company", "Item", "Amount", "Sold on", "Path", "Evil", "Nav"]
    assert '<td class="align-center">1,234.50</td>' in page
    assert '<td class="align-center">(5.00)</td>' in page
    assert '<td class="align-right">5 Nov 2026</td>' in page
    assert "shop.example/a</td>" not in page  # the hidden column's cells


def test_links_are_built_from_the_row(strata: Strata) -> None:
    rpt = _setup(strata)
    page = _page(strata, f"/r/{rpt}.html")
    # {Col}: encoded; link text from its own template; a new tab
    assert '<a href="/nav?item=A%201%2F2" target="_blank" rel="noopener">Open A 1/2</a>' in page
    assert '<a href="/nav?item=B%26C" target="_blank" rel="noopener">Open B&amp;C</a>' in page
    # {+Col}: as it is, from a hidden column; NULL there = plain text
    assert '<td><a href="https://shop.example/a">AU</a></td>' in page
    assert "<td>NZ</td>" in page
    # neither a scheme nor /: within Strata
    assert '<a href="/r/x.html?p=%2Fnav%3Fitem%3DA">/nav?item=A</a>' in page


def test_a_link_that_may_not_be_followed_shows_as_text(strata: Strata) -> None:
    rpt = _setup(strata)
    page = _page(strata, f"/r/{rpt}.html")
    assert "<td>javascript:alert(1)</td>" in page  # a {+Col} value with a refused scheme
    # A tab (browsers drop it: java<TAB>script: would run) is encoded, so the address
    # has no scheme at all and stays within Strata.
    assert '<a href="/java%09script:alert(2)">' in page
    assert 'href="javascript' not in page and 'href="java\t' not in page
    assert "<td>navision://client/item/A</td>" in page  # not allowed until an admin adds it
    strata.set_config({"links.extra_schemes": "navision"})
    page = _page(strata, f"/r/{rpt}.html")
    assert '<a href="navision://client/item/A">navision://client/item/A</a>' in page


def test_search_skips_hidden_columns_filters_do_not(strata: Strata) -> None:
    rpt = _setup(strata)
    assert "0 rows" in _page(strata, f"/r/{rpt}.html?q=shop")  # only in ItemURL
    assert json.loads(_page(strata, f"/r/{rpt}.json?q=shop")) == []  # downloads alike
    rows = json.loads(_page(strata, f"/r/{rpt}.json?q=AU"))
    assert [r["Company"] for r in rows] == ["AU"]
    rows = json.loads(_page(strata, f"/r/{rpt}.json?ItemURL=https://shop.example/a"))
    assert [r["Company"] for r in rows] == ["AU"]


def test_the_run_page_uses_the_reports_settings_as_they_are_now(strata: Strata) -> None:
    rpt = _setup(strata)
    run = _run_now(strata, rpt)
    page = _page(strata, f"/runs/{run.uuid}")
    assert '<td class="align-center">1,234.50</td>' in page
    assert '<a href="/nav?item=A%201%2F2" target="_blank" rel="noopener">Open A 1/2</a>' in page
    _import(strata, LOOK.replace('format = "#,##0.00;(#,##0.00)"', 'format = "0"'))
    page = _page(strata, f"/runs/{run.uuid}")
    assert '<td class="align-center">1235</td>' in page


def test_mail_has_formats_and_hides_columns_but_has_no_links(
    strata: Strata, mail: list[Any]
) -> None:
    rpt = _setup(strata)
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _fire(strata)
    [sent] = mail
    assert re.search(r'<td style="[^"]*text-align:center;">1,234\.50</td>', sent.html)
    assert ">5 Nov 2026</td>" in sent.html
    assert ">ItemURL</th>" not in sent.html and "shop.example/a" not in sent.html
    assert ">A 1/2</td>" in sent.html and "Open A" not in sent.html
    assert "<a " not in sent.html.split("<table", 1)[1].split("</table>", 1)[0]


def test_xlsx_carries_formats_and_alignment_and_keeps_hidden_columns(strata: Strata) -> None:
    rpt = _setup(strata)
    resp = strata.client.get(f"/r/{rpt}.xlsx")
    assert resp.status_code == 200
    ws = load_workbook(io.BytesIO(resp.get_data())).active
    assert ws is not None
    head = [c.value for c in ws[1]]
    assert "ItemURL" in head  # kept
    amount = ws.cell(row=2, column=head.index("Amount") + 1)
    assert amount.number_format == "#,##0.00;(#,##0.00)"
    assert amount.alignment.horizontal == "center"
    sold = ws.cell(row=2, column=head.index("Sold on") + 1)
    assert sold.number_format == "d mmm yyyy"
    item = ws.cell(row=2, column=head.index("Item") + 1)
    assert item.value == "A 1/2" and item.hyperlink is None  # no links in downloads


def test_csv_has_raw_values_and_every_column(strata: Strata) -> None:
    rpt = _setup(strata)
    text = _page(strata, f"/r/{rpt}.csv")
    assert text.splitlines()[0].startswith("Company,Item,ItemURL,Amount")
    assert "1234.50" in text and "1,234.50" not in text


def test_a_setting_that_no_longer_fits_is_skipped_not_fatal(strata: Strata) -> None:
    rpt = _setup(strata)
    resp = strata.client.post(
        f"/datamodules/reports/{rpt}/edit",
        data={
            "name": "Lines",
            "body_sql": 'SELECT "Company", CAST("Amount" AS VARCHAR) AS "Amount" FROM lines',
        },
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    page = _page(strata, f"/r/{rpt}.html")
    assert '<td class="align-center">1234.50</td>' in page  # its format is for numbers
    assert "<td>AU</td>" in page  # ItemURL is gone: no link


@pytest.mark.parametrize(
    ("address", "goes_to"),
    [
        ("https://x.example/a", "https://x.example/a"),
        ("mailto:jo@example.com", "mailto:jo@example.com"),
        ("/nav?item=A", "/nav?item=A"),
        ("r/abc.html?x=1", "/strata/r/abc.html?x=1"),
        ("JavaScript:alert(1)", None),
        ("data:text/html,x", None),
        ("//evil.example/x", None),  # another site, no scheme
        ("/\\evil.example", None),  # browsers read /\ as //
        ("\\\\evil.example", None),
        (" https://x.example", None),
        ("https://x.example/a\tb", None),
        ("", None),
    ],
)
def test_where_a_filled_link_may_go(address: str, goes_to: str | None) -> None:
    from strata.services.link_template import DEFAULT_SCHEMES, safe_address

    assert safe_address(address, "/strata/", DEFAULT_SCHEMES) == goes_to
