"""The exceptions overview (/checks): flagged first, filters, next run, data health."""

import re

from tests.conftest import Strata

NEGATIVE = "SELECT * FROM sales WHERE revenue < 0"
BIG = "SELECT * FROM sales WHERE revenue > 200"


def _names(page: str) -> list[str]:
    return re.findall(r'<td><a href="/r/[0-9a-f-]{36}\.html">([^<]+)</a></td>', page)


def test_the_overview_lists_checks_flagged_first(strata: Strata) -> None:
    sales = strata.new_datamodule()
    strata.build(sales)
    big = strata.new_report(sales, body=BIG, name="Big sales", macro="big", check="info")
    neg = strata.new_report(sales, body=NEGATIVE, name="Negative", macro="neg", check="error")
    strata.new_report(sales, name="Plain report", macro="plain")  # not a check
    broken = strata.new_datamodule(path="ops/broken", script="SELECT * FROM missing_table")
    strata.new_report(broken, body="SELECT 1 AS x", name="On broken", macro="onb", check="warning")
    strata.build(broken)
    strata.client.post(f"/reports/{big}/run")  # 1 row: flagged
    strata.client.post(f"/reports/{neg}/run")  # 0 rows: not flagged
    strata.client.post(
        f"/datamodules/reports/{big}/schedules/new",
        data={
            "name": "daily",
            "kind": "every",
            "every_n": "15",
            "every_unit": "minutes",
            "timezone": "Pacific/Auckland",
            "enabled_present": "1",
            "enabled": "1",
            "recipients": "ops@example.com",
        },
    )

    page = strata.client.get("/checks").get_data(as_text=True)
    assert _names(page) == ["Big sales", "Negative", "On broken"]  # flagged, then error, warning
    assert 'class="check-flagged"' in page and page.count("check-flagged") == 1
    assert page.count("not scheduled") == 2 and page.count("<small>never</small>") == 1
    assert '<span class="health health-failing" title="' in page and "missing_table" in page
    assert '<a href="/checks">Checks</a>' in page

    assert _names(strata.client.get("/checks?severity=error").get_data(as_text=True)) == [
        "Negative"
    ]
    assert _names(strata.client.get("/checks?flagged=1").get_data(as_text=True)) == ["Big sales"]


def test_the_overview_needs_a_login(strata: Strata) -> None:
    assert strata.client.get("/checks", headers={"X-Anon": "1"}).status_code == 302
    page = strata.client.get("/checks", headers={"X-User": "viewer"}).get_data(as_text=True)
    assert "No checks yet" in page
