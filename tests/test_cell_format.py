"""Plain cells: what a report shows with no format, and where its columns align.

User decision 2026-11-25: no hidden precision. Decimals show exactly as stored, trailing
zeros included (a NAV DECIMAL(38,20) shows all 20 places until a format is picked); no
thousands separators; floats in their shortest form; dates, times and timestamps ISO.
Numbers, dates and times align right, on the page, the run page and in mail.
"""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from strata.services import cell_format
from tests.conftest import Strata
from tests.test_checks import _run_now
from tests.test_notify import _fire, _schedule

AEST = timezone(timedelta(hours=10))


@pytest.mark.parametrize(
    ("value", "duck_type", "text"),
    [
        (None, "VARCHAR", ""),
        ("North", "VARCHAR", "North"),
        (True, "BOOLEAN", "true"),
        (False, "BOOLEAN", "false"),
        (1234567, "INTEGER", "1234567"),
        (-5, "BIGINT", "-5"),
        (Decimal("12.50"), "DECIMAL(18,2)", "12.50"),
        (Decimal("-1234567.891"), "DECIMAL(18,3)", "-1234567.891"),
        (Decimal("0E-20"), "DECIMAL(38,20)", "0.00000000000000000000"),
        (Decimal("1E+2"), "DECIMAL(18,0)", "100"),
        (0.1, "DOUBLE", "0.1"),
        (1e20, "DOUBLE", "1e+20"),
        (0.10000000149011612, "FLOAT", "0.1"),  # a 32-bit 0.1 as Python gets it
        (date(2026, 11, 25), "DATE", "2026-11-25"),
        (datetime(2026, 11, 25), "TIMESTAMP", "2026-11-25 00:00:00"),  # midnight kept
        (datetime(2026, 11, 25, 14, 30, 0, 500000), "TIMESTAMP", "2026-11-25 14:30:00.5"),
        (datetime(2026, 11, 25, 14, 30, 0, 123456), "TIMESTAMP", "2026-11-25 14:30:00.123456"),
        (
            datetime(2026, 11, 25, 14, 30, tzinfo=AEST),
            "TIMESTAMP WITH TIME ZONE",
            "2026-11-25 14:30:00+10:00",
        ),
        (
            datetime(2026, 11, 25, 4, 30, tzinfo=UTC),
            "TIMESTAMP WITH TIME ZONE",
            "2026-11-25 04:30:00+00:00",
        ),
        (time(14, 30), "TIME", "14:30:00"),
        (time(14, 30, 0, 250000), "TIME", "14:30:00.25"),
    ],
)
def test_plain_text(value: Any, duck_type: str, text: str) -> None:
    assert cell_format.plain(value, duck_type) == text


@pytest.mark.parametrize(
    ("duck_type", "align"),
    [
        ("TINYINT", "right"),
        ("UBIGINT", "right"),
        ("HUGEINT", "right"),
        ("BIGNUM", "right"),
        ("DECIMAL(38,20)", "right"),
        ("FLOAT", "right"),
        ("DOUBLE", "right"),
        ("DATE", "right"),
        ("TIMESTAMP", "right"),
        ("TIMESTAMP_NS", "right"),
        ("TIMESTAMP WITH TIME ZONE", "right"),
        ("TIME", "right"),
        ("INTERVAL", "right"),
        ("VARCHAR", "left"),
        ("BOOLEAN", "left"),
        ("UUID", "left"),
        ("INTEGER[]", "left"),
        ("STRUCT(a INTEGER)", "left"),
    ],
)
def test_default_alignment(duck_type: str, align: str) -> None:
    assert cell_format.default_align(duck_type) == align


TYPED_SCRIPT = """
CREATE TABLE typed AS
SELECT 'Widget' AS name, 12.50::DECIMAL(18,2) AS amount, 0::DECIMAL(38,20) AS nav_cost,
       1234567 AS qty, true AS active, 0.1::FLOAT AS ratio, DATE '2026-11-25' AS sold_on,
       TIMESTAMP '2026-11-25 00:00:00' AS stamped;
"""
TYPED_CELLS = (
    "<td>Widget</td>"
    '<td class="align-right">12.50</td>'
    '<td class="align-right">0.00000000000000000000</td>'
    '<td class="align-right">1234567</td>'
    "<td>true</td>"
    '<td class="align-right">0.1</td>'
    '<td class="align-right">2026-11-25</td>'
    '<td class="align-right">2026-11-25 00:00:00</td>'
)


def _typed_report(strata: Strata) -> str:
    dm_uuid = strata.new_datamodule(script=TYPED_SCRIPT)
    strata.build(dm_uuid)
    return strata.new_report(dm_uuid, body="SELECT * FROM typed", name="Typed")


def _header_classes(page: str) -> dict[str, str]:
    return {
        name: cls
        for cls, name in re.findall(r'<th class="([^"]*)"><a\s[^>]*>([^<]+)</a></th>', page)
    }


def test_the_page_shows_plain_cells_aligned_by_type(strata: Strata) -> None:
    rpt = _typed_report(strata)
    page = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    assert TYPED_CELLS in page
    headers = _header_classes(page)
    assert headers["name"] == "align-left" and headers["active"] == "align-left"
    assert headers["amount"] == "align-right" and headers["stamped"] == "align-right"


def test_a_sorted_header_keeps_its_alignment(strata: Strata) -> None:
    rpt = _typed_report(strata)
    page = strata.client.get(f"/r/{rpt}.html?sort=-amount").get_data(as_text=True)
    assert _header_classes(page)["amount"] == "align-right sort-desc"


def test_the_run_page_shows_the_same_cells(strata: Strata) -> None:
    rpt = _typed_report(strata)
    run = _run_now(strata, rpt)
    page = strata.client.get(f"/runs/{run.uuid}").get_data(as_text=True)
    assert TYPED_CELLS in page


def test_mail_shows_plain_cells_aligned_by_type(strata: Strata, mail: list[Any]) -> None:
    rpt = _typed_report(strata)
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _fire(strata)
    [sent] = mail
    assert ">0.00000000000000000000</td>" in sent.html
    assert re.search(r'<th style="[^"]*text-align:right;[^"]*">amount</th>', sent.html)
    assert re.search(r'<th style="[^"]*text-align:left;[^"]*">name</th>', sent.html)
    assert re.search(r'<td style="[^"]*text-align:right;">12\.50</td>', sent.html)
    assert re.search(r'<td style="[^"]*text-align:left;">true</td>', sent.html)
