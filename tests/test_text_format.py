"""Markdown written by people (report descriptions) and suggested local names."""

import pytest

from strata.services.text_format import local_name, markdown
from tests.conftest import Strata


def test_markdown_formats_and_keeps_people_safe() -> None:
    html = str(
        markdown(
            "Fix it **today**:\n\n- one\n- two\n\n"
            "[ok](https://example.com) [bad](javascript:alert(1)) ![logo](https://x.test/i.png)"
            "\n\n<script>alert(1)</script>"
        )
    )
    assert "<strong>today</strong>" in html and "<li>one</li>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html  # raw HTML is shown, not run
    assert '<a rel="noopener noreferrer" href="https://example.com">ok</a>' in html
    assert 'href="#harmful-link"' in html and "javascript:" not in html
    assert "<img" not in html and "logo" in html  # an image shows as its alt text
    assert markdown("") == "" and markdown(None) == ""


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("ref/companies", "companies"),
        ("checks/bad-items", "bad_items"),
        ("navibrs_ro", "navibrs_ro"),
        ("2026/q1 sales", "q1_sales"),
        ("x/9lives", "_9lives"),
        ("", ""),
    ],
)
def test_local_names_come_from_the_last_part(text: str, name: str) -> None:
    assert local_name(text) == name


def test_the_datamodule_page_suggests_local_names(strata: Strata) -> None:
    child = strata.new_datamodule(path="checks/child")
    strata.new_datamodule(path="ref/company-list")
    page = strata.client.get(f"/datamodules/{child}/edit").get_data(as_text=True)
    assert 'data-fills="local_name"' in page
    assert 'data-local-name="company_list">ref/company-list</option>' in page


def test_the_report_form_adds_argument_rows_on_demand(strata: Strata) -> None:
    dm = strata.new_datamodule()
    new = strata.client.get(f"/datamodules/{dm}/reports/new").get_data(as_text=True)
    body = new.split('<tbody id="arg-rows">', 1)[1].split("</tbody>", 1)[0]
    assert "<tr>" not in body  # no blank rows
    assert '<template id="arg-row-template"><tr>' in new
    assert 'data-add-row="arg-row-template" data-into="arg-rows">Add argument</button>' in new
    rpt = strata.new_report(
        dm,
        body="SELECT * FROM sales WHERE region = $region",
        args=[{"name": "region", "type": "text", "widget": "input", "default": "North"}],
    )
    edit = strata.client.get(f"/datamodules/reports/{rpt}/edit").get_data(as_text=True)
    rows = edit.split('<tbody id="arg-rows">', 1)[1].split("</tbody>", 1)[0]
    assert rows.count("<tr>") == 1 and 'value="region"' in rows and "data-remove-row" in rows


def test_a_description_is_markdown_on_the_report_page(strata: Strata) -> None:
    dm = strata.new_datamodule()
    strata.build(dm)
    resp = strata.client.post(
        f"/datamodules/{dm}/reports/new",
        data={
            "name": "Big",
            "description": "Ask **finance**:\n\n- check the price\n- <b>then</b> fix",
            "body_sql": "SELECT * FROM sales",
        },
    )
    assert resp.status_code == 302
    with strata.app.app_context():
        from strata.models.datamodule import Datamodule
        from strata.models.report import Report

        (report,) = Report.get_for_datamodule(Datamodule.get_by_uuid(dm).id)  # type: ignore[union-attr]
    page = strata.client.get(f"/r/{report.uuid}.html").get_data(as_text=True)
    block = page.split('<div class="report-description">', 1)[1].split("</div>", 1)[0]
    assert "<strong>finance</strong>" in block and "<li>check the price</li>" in block
    assert "&lt;b&gt;then&lt;/b&gt;" in block
