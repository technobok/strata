"""Totals and subtotals (services/totals.py): over every row the view matches, footers
after each group (on the page where the group ends), the grand total after the last
row of the last page; in mail only when every row fits."""

import re
from typing import Any

import pytest

from strata.services import query_filter, totals
from tests.conftest import Strata
from tests.test_format_page import _url
from tests.test_notify import _fire, _schedule
from tests.test_presentation import _import, _report

SCRIPT = """
CREATE TABLE sales AS SELECT * FROM (VALUES
  ('AU', 'North', 'a1', 10.00::DECIMAL(18,2), 1, DATE '2026-01-01'),
  ('AU', 'North', 'a2', 20.00::DECIMAL(18,2), 2, DATE '2026-01-02'),
  ('AU', 'South', 'a3', 30.00::DECIMAL(18,2), 3, DATE '2026-01-03'),
  ('NZ', 'North', 'n1',  5.00::DECIMAL(18,2), 4, DATE '2026-01-04'),
  ('NZ', 'South', 'n2',  7.00::DECIMAL(18,2), 5, DATE '2026-01-05'),
  (NULL, 'North', 'x1',  1.00::DECIMAL(18,2), 6, DATE '2026-01-06')
) AS t("Company", "Branch", "Item", "Amount", "Qty", "Day");
"""

LOOK = """
path = "test/totals"

[[report]]
name = "Sales"
body = "SELECT * FROM sales"
subtotals_by = {groups}
total_row = true

[[report.column]]
name = "Amount"
format = "#,##0.00"
total = "sum"

[[report.column]]
name = "Qty"
total = "avg"

[[report.column]]
name = "Item"
total = "count_distinct"

[[report.column]]
name = "Day"
format = "d mmm"
total = "max"
"""


@pytest.fixture
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(query_filter, "PAGE_SIZE", 2)


def _setup(strata: Strata, groups: str = '["Company"]') -> str:
    dm_uuid = strata.new_datamodule(path="test/totals", script=SCRIPT)
    strata.build(dm_uuid)
    out = _import(strata, LOOK.replace("{groups}", groups))
    assert out.refused is None, out.refused
    return _report(strata, "Sales", "test/totals").uuid


def _rows(page: str) -> list[tuple[str, list[str]]]:
    """(kind, cell texts) per row of the results table."""
    results = page.split('<div class="results-container">', 1)[1]
    body = results.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    out = []
    for cls, inner in re.findall(r'<tr(?: class="([^"]*)")?>(.*?)</tr>', body, re.S):
        cells = [re.sub(r"<[^>]+>", "", c) for c in re.findall(r"<td[^>]*>(.*?)</td>", inner, re.S)]
        out.append((cls.split(" ")[0] if cls else "data", cells))
    return out


def _page(strata: Strata, rpt: str, query: str = "") -> list[tuple[str, list[str]]]:
    resp = strata.client.get(f"/r/{rpt}.html{query}")
    assert resp.status_code == 200
    return _rows(resp.get_data(as_text=True))


# Company, Branch, Item, Amount, Qty, Day
AU_TOTAL = ("subtotal", ["AU total", "", "3", "60.00", "2.0", "3 Jan"])
NZ_TOTAL = ("subtotal", ["NZ total", "", "2", "12.00", "4.5", "5 Jan"])
BLANK_TOTAL = ("subtotal", ["(blank) total", "", "1", "1.00", "6.0", "6 Jan"])
GRAND = ("total", ["Total", "", "6", "73.00", "3.5", "6 Jan"])


def test_a_subtotal_follows_its_group_on_the_page_where_it_ends(
    strata: Strata, small_pages: None
) -> None:
    rpt = _setup(strata)
    assert _page(strata, rpt, "?sort=Item") == [
        ("data", ["AU", "North", "a1", "10.00", "1", "1 Jan"]),
        ("data", ["AU", "North", "a2", "20.00", "2", "2 Jan"]),
    ]  # AU goes on: no subtotal yet
    assert _page(strata, rpt, "?sort=Item&page=2") == [
        ("data", ["AU", "South", "a3", "30.00", "3", "3 Jan"]),
        AU_TOTAL,  # over all of AU's rows, not this page's
        ("data", ["NZ", "North", "n1", "5.00", "4", "4 Jan"]),
    ]
    assert _page(strata, rpt, "?sort=Item&page=3") == [
        ("data", ["NZ", "South", "n2", "7.00", "5", "5 Jan"]),
        NZ_TOTAL,
        ("data", ["", "North", "x1", "1.00", "6", "6 Jan"]),
        BLANK_TOTAL,  # a group whose value is empty
        GRAND,  # after the last row of the last page only
    ]


def test_rows_sort_by_group_first_and_a_group_header_flips_it(strata: Strata) -> None:
    rpt = _setup(strata)
    rows = _page(strata, rpt, "?sort=-Item")  # the view's sort applies within groups
    assert [r[1][2] for r in rows if r[0] == "data"] == ["a3", "a2", "a1", "n2", "n1", "x1"]
    rows = _page(strata, rpt, "?sort=-Company,Item")
    assert [r[1][0] for r in rows if r[0] == "subtotal"] == [
        "NZ total",
        "AU total",
        "(blank) total",
    ]


def test_totals_cover_the_filtered_rows(strata: Strata) -> None:
    rpt = _setup(strata)
    rows = _page(strata, rpt, "?Branch=North&sort=Item")
    assert ("subtotal", ["AU total", "", "2", "30.00", "1.5", "2 Jan"]) in rows
    assert rows[-1] == ("total", ["Total", "", "4", "36.00", "3.25", "6 Jan"])


def test_nested_subtotals(strata: Strata) -> None:
    rpt = _setup(strata, '["Company", "Branch"]')
    labels = [r[1][:2] for r in _page(strata, rpt, "?sort=Item") if r[0] != "data"]
    assert labels == [
        ["AU", "North total"],
        ["AU", "South total"],
        ["AU total", ""],
        ["NZ", "North total"],
        ["NZ", "South total"],
        ["NZ total", ""],
        ["(blank)", "North total"],
        ["(blank) total", ""],
        ["Total", ""],
    ]


def test_a_total_row_without_subtotals_is_on_the_last_page_only(
    strata: Strata, small_pages: None
) -> None:
    rpt = _setup(strata, "[]")
    assert all(kind == "data" for kind, _ in _page(strata, rpt, "?sort=Item"))
    assert _page(strata, rpt, "?sort=Item&page=3")[-1] == GRAND


def test_mail_shows_subtotals_when_every_row_fits(strata: Strata, mail: list[Any]) -> None:
    rpt = _setup(strata)
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _fire(strata)
    [sent] = mail
    assert re.search(r'font-weight:700;">AU total</td>', sent.html)
    assert re.search(r'font-weight:700;">73\.00</td>', sent.html)


def test_mail_with_more_rows_than_fit_has_one_total_row(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule(
        path="test/totals",
        script="CREATE TABLE sales AS SELECT 'AU' AS \"Company\", 'North' AS \"Branch\", "
        '\'i\' || i AS "Item", i::DECIMAL(18,2) AS "Amount", i AS "Qty", '
        "DATE '2026-01-01' AS \"Day\" FROM range(1, 26) t(i);",
    )
    strata.build(dm_uuid)
    assert _import(strata, LOOK.replace("{groups}", '["Company"]')).refused is None
    rpt = _report(strata, "Sales", "test/totals").uuid
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _fire(strata)
    [sent] = mail
    assert "The first 20 of 25 rows" in sent.html
    assert "AU total" not in sent.html
    assert re.search(r'font-weight:700;">Total of all 25 rows</td>', sent.html)
    assert re.search(r'font-weight:700;">325\.00</td>', sent.html)  # 1 + … + 25


def test_the_format_page_sets_totals(strata: Strata) -> None:
    rpt = _setup(strata, "[]")
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    totals_for = re.search(r'<select name="c-0-total".*?</select>', page, re.S)
    assert totals_for is not None  # Company (text): no sum or avg
    assert "sum" not in totals_for.group(0) and "count distinct" in totals_for.group(0)
    form: dict[str, Any] = {
        "totals_present": "1",
        "subtotals_by": ["Branch", ""],
        "total_row": "on",
    }
    for n, name in enumerate(["Company", "Branch", "Item", "Amount", "Qty", "Day"]):
        form[f"c-{n}-name"] = name
        form[f"c-{n}-align"] = "auto"
    form["c-3-total"] = "sum"
    assert strata.client.post(_url(rpt), data=form).status_code == 302
    from strata.services.presentation import from_json

    stored = from_json(_report(strata, "Sales", "test/totals").presentation)
    assert stored.subtotals_by == ["Branch"] and stored.total_row
    assert [c.name for c in stored.columns if c.total] == ["Amount"]
    form["c-3-total"] = ""
    refused = strata.client.post(_url(rpt), data=form).get_data(as_text=True)
    assert "Subtotals need a column with a total" in refused


def test_plan_places_footers() -> None:
    rows = [("a", 1), ("a", 2), ("b", 3)]
    plan = totals.plan(rows, ("b", 4), [0], total_row=True)  # b goes on
    assert [(t.after, t.level, t.key) for t in plan] == [(1, 1, ("a",))]
    plan = totals.plan(rows, None, [0], total_row=True)  # the data ends here
    assert [(t.after, t.level, t.key) for t in plan] == [(1, 1, ("a",)), (2, 1, ("b",)), (2, 0, ())]
    assert totals.plan(rows, rows[-1], [], total_row=True) == []  # more rows follow


def test_a_group_ending_on_a_pages_last_row_gets_its_subtotal_there(
    strata: Strata, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The look-ahead row tells that AU ends at this page's last row."""
    monkeypatch.setattr(query_filter, "PAGE_SIZE", 3)
    rpt = _setup(strata)
    first = _page(strata, rpt, "?sort=Item")
    assert [r[1][2] for r in first if r[0] == "data"] == ["a1", "a2", "a3"]
    assert first[-1] == AU_TOTAL
    assert AU_TOTAL not in _page(strata, rpt, "?sort=Item&page=2")


def test_a_count_shows_plain_whatever_its_columns_format(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(path="test/totals", script=SCRIPT)
    strata.build(dm_uuid)
    look = LOOK.replace("{groups}", "[]").replace(
        'format = "#,##0.00"\ntotal = "sum"', 'format = "#,##0.00"\ntotal = "count"'
    )
    assert _import(strata, look).refused is None
    rpt = _report(strata, "Sales", "test/totals").uuid
    assert _page(strata, rpt)[-1][1][3] == "6"  # not "6.00"
