"""Formatting after a rebuild (report_service.check_reports): a setting the new data no
longer fits is recorded in report.presentation_problem and shown to the report's owners
(report page, datamodule page, Format page); the report still shows, that part plain;
nobody is mailed; the next build that fits clears it."""

from typing import Any

from tests.conftest import Strata, as_user
from tests.test_format_page import _url
from tests.test_formatting_render import LOOK, SCRIPT
from tests.test_presentation import _import, _report
from tests.test_report_problems import _scheduled_build

# The same table without "Path" (a link and its template) and "Sold on" (a format).
WITHOUT = """
CREATE TABLE lines AS SELECT * FROM (VALUES
  ('AU', 'A 1/2', 'https://shop.example/a', 1234.5::DECIMAL(18,2),
   'javascript:alert(1)', 'navision://client/item/A')
) AS t("Company", "Item", "ItemURL", "Amount", "Evil", "Nav");
"""


def _setup(strata: Strata) -> tuple[str, str]:
    dm_uuid = strata.new_datamodule(path="test/lines", script=SCRIPT)
    strata.build(dm_uuid)
    assert _import(strata, LOOK).refused is None
    return dm_uuid, _report(strata, "Lines", "test/lines").uuid


def _problem(strata: Strata) -> str | None:
    return _report(strata, "Lines", "test/lines").presentation_problem


def test_a_rebuild_that_drops_a_formatted_column_is_recorded_and_shown(
    strata: Strata, mail: list[Any]
) -> None:
    dm_uuid, rpt = _setup(strata)
    assert _problem(strata) is None
    strata.set_script(dm_uuid, WITHOUT)
    _scheduled_build(strata, dm_uuid)  # a scheduled build: one that would mail

    problem = _problem(strata)
    assert problem is not None
    assert "'Sold on' is not a column of this report" in problem
    assert "'Path' is not a column of this report" in problem
    assert not mail  # never mailed

    page = strata.client.get(f"/r/{rpt}.html")
    assert page.status_code == 200  # the report still shows ...
    text = page.get_data(as_text=True)
    assert "Some of this report's formatting no longer fits its data" in text
    assert '<td class="align-center">1,234.50</td>' in text  # ... with what still fits
    viewer = strata.client.get(f"/r/{rpt}.html", headers=as_user("viewer"))
    assert "formatting no longer fits" not in viewer.get_data(as_text=True)

    dm_page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert "formatting no longer fits</a>" in dm_page
    format_page = strata.client.get(_url(rpt)).get_data(as_text=True)
    assert "Found after the report's last build or SQL change" in format_page
    assert "No longer in the result" in format_page


def test_the_next_build_that_fits_clears_it(strata: Strata) -> None:
    dm_uuid, rpt = _setup(strata)
    strata.set_script(dm_uuid, WITHOUT)
    strata.build(dm_uuid)
    assert _problem(strata) is not None
    strata.set_script(dm_uuid, SCRIPT)
    strata.build(dm_uuid)
    assert _problem(strata) is None
    assert "no longer fits" not in strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)


def test_settings_imported_before_the_first_build_are_checked_by_it(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(path="test/lines", script=SCRIPT)  # not built
    bad = LOOK.replace(
        'name = "Sold on"\nformat = "d mmm yyyy"', 'name = "Sold on"\nformat = "0.00"'
    )
    assert _import(strata, bad).refused is None  # types unknown yet
    strata.build(dm_uuid)
    problem = _problem(strata)
    assert problem is not None
    assert "Sold on: format: This column holds dates or times" in problem


def test_a_report_that_no_longer_runs_shows_that_problem_only(strata: Strata) -> None:
    dm_uuid, _rpt = _setup(strata)
    strata.set_script(dm_uuid, "CREATE TABLE other AS SELECT 1 AS x")
    strata.build(dm_uuid)
    report = _report(strata, "Lines", "test/lines")
    assert report.problem is not None
    assert report.presentation_problem is None
