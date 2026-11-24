"""A report's name is its title (unique per datamodule, in any case); its macro name
is made from the name once and kept, because a workbook's query selects the xlsx
sheet (``<macro>``) and table (``<macro>_table``) by it. An optional description
shows on the report page and in its mail.
"""

import io
from typing import Any

import openpyxl
import pytest

from strata.services import report_service
from tests.conftest import Strata
from tests.test_notify import _fire, _schedule


@pytest.mark.parametrize(
    ("name", "taken", "macro"),
    [
        ("Bad item department", [], "bad_item_department"),
        ("Bad item — département", [], "bad_item_departement"),
        ("  Sales (NZ) 2026!  ", [], "sales_nz_2026"),
        ("2026 sales", [], "report_2026_sales"),
        ("!!!", [], "report"),
        ("Range", [], "range_report"),  # a DuckDB function: it would shadow range()
        ("Select", [], "select_report"),  # a keyword: CREATE MACRO select … fails
        ("Sales", ["sales", "SALES_2"], "sales_3"),
        ("x" * 60, [], "x" * report_service.MACRO_MAX),
    ],
)
def test_the_macro_name_is_made_from_the_name(name: str, taken: list[str], macro: str) -> None:
    assert report_service.macro_name_for(name, taken) == macro


def _report(strata: Strata, rpt: str) -> Any:
    with strata.app.app_context():
        from strata.models.report import Report

        report = Report.get_by_uuid(rpt)
        assert report is not None
        return report


def test_a_rename_keeps_the_macro_name(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid, name="Bad item department")
    assert _report(strata, rpt).macro_name == "bad_item_department"
    resp = strata.client.post(
        f"/datamodules/reports/{rpt}/edit",
        data={"name": "Items in the wrong department", "body_sql": "SELECT * FROM sales"},
    )
    assert resp.status_code == 302
    renamed = _report(strata, rpt)
    assert renamed.name == "Items in the wrong department"
    assert renamed.macro_name == "bad_item_department"
    page = strata.client.get(f"/datamodules/reports/{rpt}/edit").get_data(as_text=True)
    assert "sheet <code>bad_item_department</code>" in page
    assert "table <code>bad_item_department_table</code>" in page


def test_two_reports_cannot_share_a_name_in_any_case(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.new_report(dm_uuid, name="Sales")
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/reports/new",
        data={"name": "SALES", "body_sql": "SELECT 1 AS n"},
    )
    assert resp.status_code == 200
    assert "test/sales already has a report named &#39;SALES&#39;" in resp.get_data(as_text=True)


def test_another_datamodule_may_reuse_a_name(strata: Strata) -> None:
    first = strata.new_datamodule()
    second = strata.new_datamodule(path="test/other")
    a = strata.new_report(first, name="Sales")
    b = strata.new_report(second, name="Sales")
    assert _report(strata, a).macro_name == _report(strata, b).macro_name == "sales"


def _sheets_and_tables(data: bytes) -> list[tuple[str, list[str]]]:
    wb = openpyxl.load_workbook(io.BytesIO(data))
    return [(ws.title, list(ws.tables.keys())) for ws in wb.worksheets]


def test_the_xlsx_sheet_and_table_are_named_after_the_macro(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid, name="Bad item department")
    resp = strata.client.get(f"/r/{rpt}.xlsx")
    assert resp.status_code == 200
    assert 'filename="bad_item_department.xlsx"' in resp.headers["Content-Disposition"]
    assert _sheets_and_tables(resp.get_data()) == [
        ("bad_item_department", ["bad_item_department_table"])
    ]

    run = strata.client.post(f"/reports/{rpt}/run")  # a saved run downloads the same way
    run_uuid = run.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
    data = strata.client.get(f"/runs/{run_uuid}.xlsx")
    assert data.status_code == 200, data.get_data(as_text=True)[:200]
    assert _sheets_and_tables(data.get_data()) == [
        ("bad_item_department", ["bad_item_department_table"])
    ]


def test_the_description_shows_on_the_page_and_in_mail(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/reports/new",
        data={
            "name": "Big sales",
            "description": "Sales over 100: check the price with the account manager.",
            "body_sql": "SELECT * FROM sales WHERE revenue > 100",
            "is_check": "on",
            "severity": "warning",
        },
    )
    assert resp.status_code == 302
    with strata.app.app_context():
        from strata.models.datamodule import Datamodule
        from strata.models.report import Report

        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        (report,) = Report.get_for_datamodule(dm.id)
    page = strata.client.get(f"/r/{report.uuid}.html").get_data(as_text=True)
    assert "check the price with the account manager" in page
    assert _schedule(strata, report.uuid, "jsmith").status_code == 302
    _fire(strata)
    (sent,) = mail
    assert "<p>Sales over 100: check the price with the account manager.</p>" in sent.html
