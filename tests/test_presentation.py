"""A report's presentation settings (services/presentation.py): the model, its JSON,
its checks against the report's columns, and the save rules (services/authoring) that
the Format page and definition files share.

Each refusal case below has one possible cause: the valid BASE with one thing changed.
"""

import dataclasses
from typing import Any

import pytest

from strata.services import link_template, styles
from strata.services.presentation import (
    ColumnSetting,
    Presentation,
    PresentationError,
    StyleRule,
    from_dict,
    from_json,
    validate,
)
from tests.conftest import Strata

COLUMNS = [
    ("Company", "VARCHAR"),
    ("Item", "VARCHAR"),
    ("ItemURL", "VARCHAR"),
    ("Amount", "DECIMAL(18,2)"),
    ("Qty", "INTEGER"),
    ("Blocked", "BOOLEAN"),
    ("Sold on", "DATE"),
]


def base() -> Presentation:
    return Presentation(
        columns=[
            ColumnSetting("Amount", format="#,##0.00;[Red]-#,##0.00", total="sum"),
            ColumnSetting("Item", link="/nav?item={Item}", link_text="Open {Item}", new_tab=True),
            ColumnSetting("ItemURL", hidden=True),
            ColumnSetting("Sold on", format="d mmm yyyy", align="center", total="max"),
        ],
        styles=[StyleRule('"Amount" < 0', "red bold", ["Amount"])],
        subtotals_by=["Company"],
        total_row=True,
    )


def test_the_base_is_valid() -> None:
    assert validate(base(), COLUMNS) == []


def test_json_round_trip_drops_what_says_nothing() -> None:
    p = base()
    p.columns.append(ColumnSetting("Qty"))  # all defaults: says nothing
    document = p.to_json()
    assert document is not None and '"Qty"' not in document
    assert from_json(document) == base()
    assert Presentation().to_json() is None
    assert Presentation(columns=[ColumnSetting("Qty")]).to_json() is None
    assert from_json(None) == Presentation()


def _with(change: str) -> Presentation:
    p = base()
    col = {c.name: c for c in p.columns}
    rule = p.styles[0]
    match change:
        case "unknown column":
            col["Amount"].name = "Amout"
        case "two settings":
            p.columns.append(ColumnSetting("Item", align="left"))
        case "unsupported format":
            col["Amount"].format = "0.00E+00"
        case "date format on a number":
            col["Amount"].format = "yyyy"
        case "number format on a date":
            col["Sold on"].format = "#,##0"
        case "align":
            col["Amount"].align = "middle"
        case "total name":
            col["Amount"].total = "median"
        case "sum on text":
            p.columns.append(ColumnSetting("Company", total="sum"))
            p.subtotals_by = ["Qty"]  # Company groups nothing now
        case "min on boolean":
            p.columns.append(ColumnSetting("Blocked", total="min"))
        case "hidden total":
            col["Amount"].hidden = True
            rule.columns = []
        case "link syntax":
            col["Item"].link = "/nav?item={Item"
        case "link column":
            col["Item"].link = "/nav?item={Itme}"
        case "link scheme":
            col["Item"].link = "javascript:alert({Item})"
        case "hidden link":
            col["Item"].hidden = True
        case "link text without link":
            col["Item"].link = ""
            col["Item"].new_tab = False
        case "new tab without link":
            col["Item"].link = col["Item"].link_text = ""
        case "link text column":
            col["Item"].link_text = "Open {Itm}"
        case "subtotals unknown":
            p.subtotals_by = ["Compny"]
        case "subtotals twice":
            p.subtotals_by = ["Company", "Company"]
        case "subtotals hidden":
            p.subtotals_by = ["ItemURL"]
        case "subtotals on a total":
            p.subtotals_by = ["Amount"]
        case "subtotals without totals":
            col["Amount"].total = col["Sold on"].total = ""
            p.total_row = False
        case "total row without totals":
            col["Amount"].total = col["Sold on"].total = ""
            p.subtotals_by = []
        case "no condition":
            rule.when = "  "
        case "style word":
            rule.style = "rde bold"
        case "two colours":
            rule.style = "red blue"
        case "style shade":
            rule.style = "red-555"
        case "style column":
            rule.columns = ["Amonut"]
        case "style hidden column":
            rule.columns = ["ItemURL"]
        case _:
            raise AssertionError(change)
    return p


REFUSALS = [
    (
        "unknown column",
        "column:Amout:name",
        "'Amout' is not a column of this report (did you mean 'Amount'?): remove its settings",
    ),
    ("two settings", "column:Item:name", "'Item' has two sets of settings"),
    (
        "unsupported format",
        "column:Amount:format",
        "Amount: format: Scientific notation (E+) is not supported (at character 5)",
    ),
    (
        "date format on a number",
        "column:Amount:format",
        "Amount: format: This column holds numbers: y, m, d, h and s are for dates and times "
        "(at character 1)",
    ),
    (
        "number format on a date",
        "column:Sold on:format",
        "Sold on: format: This column holds dates or times: use y, m, d, h and s (at character 1)",
    ),
    ("align", "column:Amount:align", "Amount: align is auto, left, center or right, not 'middle'"),
    (
        "total name",
        "column:Amount:total",
        "Amount: total is one of sum, avg, min, max, count, count_distinct, not 'median'",
    ),
    ("sum on text", "column:Company:total", "Company: sum needs numbers; this column is VARCHAR"),
    (
        "min on boolean",
        "column:Blocked:total",
        "Blocked: min needs numbers, dates, times or text; this column is BOOLEAN",
    ),
    ("hidden total", "column:Amount:total", "Amount is hidden, so its total never shows"),
    (
        "link syntax",
        "column:Item:link",
        "Item: link: A { has no closing }: write {{ for a brace (at character 11)",
    ),
    (
        "link column",
        "column:Item:link",
        "Item: link: 'Itme' is not a column of this report (did you mean 'Item'?)",
    ),
    (
        "link scheme",
        "column:Item:link",
        "Item: link: links may go to http:, https:, mailto: addresses, not 'javascript:'",
    ),
    ("hidden link", "column:Item:link", "Item is hidden, so its link never shows"),
    ("link text without link", "column:Item:link_text", "Item: link text needs a link"),
    ("new tab without link", "column:Item:new_tab", "Item: open in a new tab needs a link"),
    (
        "link text column",
        "column:Item:link_text",
        "Item: link text: 'Itm' is not a column of this report (did you mean 'Item'?)",
    ),
    (
        "subtotals unknown",
        "subtotals_by",
        "Subtotals by 'Compny' is not a column of this report (did you mean 'Company'?)",
    ),
    ("subtotals twice", "subtotals_by", "Subtotals by 'Company' twice"),
    (
        "subtotals hidden",
        "subtotals_by",
        "'ItemURL' is hidden, but its subtotal rows are labelled in it",
    ),
    (
        "subtotals on a total",
        "column:Amount:total",
        "'Amount' groups the subtotals, so it shows their labels, not a total",
    ),
    (
        "subtotals without totals",
        "subtotals_by",
        "Subtotals need a column with a total (e.g. sum on an amount)",
    ),
    (
        "total row without totals",
        "total_row",
        "A total row needs a column with a total (e.g. sum on an amount)",
    ),
    ("no condition", "style:1:when", 'Style 1: a rule needs a condition, e.g. "Amount" < 0'),
    ("style word", "style:1:style", "Style 1: 'rde' is not a style (did you mean 'red'?)"),
    ("two colours", "style:1:style", "Style 1: Two text colours: red and blue"),
    ("style shade", "style:1:style", "Style 1: 'red-555': Pico's shades are 50, 100, 150"),
    (
        "style column",
        "style:1:columns",
        "Style 1: 'Amonut' is not a column of this report (did you mean 'Amount'?)",
    ),
    (
        "style hidden column",
        "style:1:columns",
        "Style 1: 'ItemURL' is hidden, so its style never shows",
    ),
]


@pytest.mark.parametrize(("change", "where", "message"), REFUSALS)
def test_one_problem_one_cause(change: str, where: str, message: str) -> None:
    [problem] = validate(_with(change), COLUMNS)
    assert problem.field == where
    assert problem.message.startswith(message), problem.message


def test_a_scheme_an_admin_allows_is_a_link() -> None:
    p = base()
    p.columns[1].link = "navision://client/run?item={Item}"
    [problem] = validate(p, COLUMNS)
    assert "not 'navision:'" in problem.message
    assert validate(p, COLUMNS, schemes=("http", "https", "mailto", "navision")) == []


def test_without_columns_only_what_needs_none_is_checked() -> None:
    """Before a datamodule's first build its columns are unknown: names and types wait."""
    assert validate(_with("unknown column"), None) == []
    assert validate(_with("date format on a number"), None) == []
    assert validate(_with("link column"), None) == []
    [problem] = validate(_with("unsupported format"), None)
    assert problem.field == "column:Amount:format"
    [problem] = validate(_with("style word"), None)
    assert problem.field == "style:1:style"


@pytest.mark.parametrize(
    ("data", "words"),
    [
        ({"colour": []}, "unknown key(s) colour"),
        ({"columns": [{"format": "0"}]}, "'name' is required"),
        ({"columns": [{"name": "A", "hidden": "yes"}]}, "'hidden' must be true or false"),
        ({"columns": [{"name": "A", "format": 0}]}, "'format' must be text"),
        ({"columns": [{"name": "A", "colour": "red"}]}, "unknown key(s) colour"),
        ({"styles": [{"when": "true"}]}, "'style' is required"),
        (
            {"styles": [{"when": "true", "style": "red", "columns": "A"}]},
            "'columns' must be a list of column names",
        ),
        ({"subtotals_by": "Company"}, "'subtotals_by' must be a list of column names"),
        ({"total_row": 1}, "'total_row' must be true or false"),
    ],
)
def test_a_document_of_the_wrong_shape_is_refused(data: dict[str, Any], words: str) -> None:
    with pytest.raises(PresentationError, match=words.replace("(", r"\(").replace(")", r"\)")):
        from_dict(data)


def test_link_templates() -> None:
    t = link_template.parse("https://x.example/{Customer Code}?a={+Path}&b={{literal}}")
    assert t.columns() == ["Customer Code", "Path"]
    assert t.scheme() == "https"
    assert [p.text for p in t.parts if not p.column] == [
        "https://x.example/",
        "?a=",
        "&b={literal}",
    ]
    assert link_template.parse("/nav?x={A}").scheme() == ""
    assert link_template.parse("r/abc.html").scheme() == ""
    assert link_template.parse("{+URL}").scheme() is None  # its value decides, at render
    assert link_template.parse("{URL}").scheme() == ""  # encoded: ':' cannot start a scheme
    for bad, words in [("{}", "names no column"), ("a}", "A } without a {"), ("{a", "no closing")]:
        with pytest.raises(link_template.TemplateError, match=words):
            link_template.parse(bad)


def test_style_words() -> None:
    assert styles.parse("Red  BOLD bold bg-amber") == ["red", "bold", "bg-amber"]
    assert styles.parse("red-550 bg-slate-100 strike") == ["red-550", "bg-slate-100", "strike"]
    for bad, words in [
        ("", "at least one word"),
        ("bg-red bg-blue", "Two backgrounds"),
        ("red grey-50", "Two text colours"),
        ("purpel", "did you mean 'purple'"),
        ("color:red", "is not a style"),
    ]:
        with pytest.raises(styles.StyleError, match=words):
            styles.parse(bad)


# ---------------------------------------------------------------------------
# Saving: definition files and the report form share authoring's rules
# ---------------------------------------------------------------------------

LOOK_FILE = """
path = "test/sales"

[[report]]
name = "Sales"
body = "SELECT * FROM sales"
subtotals_by = ["region"]
total_row = true

[[report.column]]
name = "revenue"
format = "#,##0.00"
total = "sum"

[[report.column]]
name = "product"
link = "https://shop.example/p/{product}"
new_tab = true

[[report.style]]
when = '{when}'
columns = []
style = "red bold"
"""


def _file(when: str = '"revenue" > 200') -> str:
    return LOOK_FILE.replace("{when}", when)


def _import(strata: Strata, text: str) -> Any:
    from strata.services import definitions

    with strata.app.app_context():
        (out,) = definitions.import_definitions([definitions.parse(text, "f.toml")], "importer")
        return out


def _report(strata: Strata, name: str = "Sales") -> Any:
    with strata.app.app_context():
        from strata.models.datamodule import Datamodule
        from strata.models.report import Report

        dm = Datamodule.get_by_path("test/sales")
        assert dm is not None
        return next(r for r in Report.get_for_datamodule(dm.id) if r.name == name)


def _built(strata: Strata) -> str:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    return dm_uuid


def test_a_file_sets_the_look_and_exports_it_back_unchanged(strata: Strata) -> None:
    _built(strata)
    out = _import(strata, _file())
    assert out.refused is None, out.refused
    stored = from_json(_report(strata).presentation)
    assert stored.subtotals_by == ["region"] and stored.total_row
    assert stored.setting("revenue").format == "#,##0.00"
    assert stored.styles == [StyleRule('"revenue" > 200', "red bold", [])]
    assert _report(strata).presentation_problem is None

    from strata.services import definitions

    with strata.app.app_context():
        from strata.models.datamodule import Datamodule

        dm = Datamodule.get_by_path("test/sales")
        assert dm is not None
        text = definitions.export_datamodule(dm)
    assert "[[report.column]]\nname = 'revenue'\nformat = '#,##0.00'\ntotal = 'sum'" in text
    again = _import(strata, text)
    assert (again.action, again.changes) == ("unchanged", [])


def test_a_file_without_look_keys_leaves_the_look_alone(strata: Strata) -> None:
    _built(strata)
    _import(strata, _file())
    plain = 'path = "test/sales"\n[[report]]\nname = "Sales"\nbody = "SELECT * FROM sales"\n'
    out = _import(strata, plain)
    assert out.refused is None
    assert from_json(_report(strata).presentation).total_row


def test_a_key_the_file_gives_replaces_only_that_part(strata: Strata) -> None:
    _built(strata)
    _import(strata, _file())
    out = _import(
        strata,
        'path = "test/sales"\n[[report]]\nname = "Sales"\nbody = "SELECT * FROM sales"\n'
        "style = []\n",
    )
    assert out.refused is None and out.changes == ["report 'Sales'"]
    stored = from_json(_report(strata).presentation)
    assert stored.styles == [] and stored.total_row and stored.setting("revenue").total == "sum"


@pytest.mark.parametrize(
    ("when", "words"),
    [
        ('"revnue" > 200', 'Referenced column "revnue" not found'),
        ('"revenue"', "Style 1: condition: a rule must be true or false"),
        ('"revenue" > ', "Style 1: condition: The condition does not parse"),
        ("true) AS r FROM sales; SELECT (true", "a rule is one condition on the row"),
    ],
)
def test_a_condition_must_bind_on_the_live_data(strata: Strata, when: str, words: str) -> None:
    _built(strata)
    out = _import(strata, _file(when))
    assert out.refused is not None and words in out.refused, out.refused
    assert "Not saved: the report's formatting has problems." in out.refused


def test_a_setting_that_does_not_fit_the_live_columns_refuses_the_file(strata: Strata) -> None:
    _built(strata)
    out = _import(strata, _file().replace('name = "revenue"', 'name = "Revenue"'))
    assert out.refused is not None
    assert "'Revenue' is not a column of this report (did you mean 'revenue'?)" in out.refused


def test_before_the_first_build_names_wait_and_syntax_does_not(strata: Strata) -> None:
    strata.new_datamodule()  # not built: no columns yet
    out = _import(strata, _file().replace('name = "revenue"', 'name = "Revenue"'))
    assert out.refused is None
    out = _import(strata, _file().replace('format = "#,##0.00"', 'format = "0.0E+0"'))
    assert out.refused is not None and "Scientific notation" in out.refused


def test_a_body_edit_keeps_the_look_and_records_what_no_longer_fits(strata: Strata) -> None:
    dm_uuid = _built(strata)
    _import(strata, _file())
    rpt = _report(strata)
    resp = strata.client.post(
        f"/datamodules/reports/{rpt.uuid}/edit",
        data={"name": "Sales", "body_sql": "SELECT region, product FROM sales"},
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    after = _report(strata)
    assert after.presentation == rpt.presentation  # never clobbered by the report form
    assert after.presentation_problem is not None
    assert "'revenue' is not a column of this report" in after.presentation_problem
    assert "Style 1: condition: " in after.presentation_problem
    assert dm_uuid


def test_0015_adds_presentation_columns(tmp_path, monkeypatch) -> None:
    """An existing report has no settings and no problem (NULL)."""
    import apsw

    from strata import db

    path = str(tmp_path / "m.sqlite3")
    every = db._migration_files
    monkeypatch.setattr(db, "_migration_files", lambda: [f for f in every() if f[0] <= 14])
    db.init_db_at(path)
    conn = apsw.Connection(path)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO datamodule (uuid, path, build_script, created_by, modified_by, "
        "created_at, modified_at) VALUES ('d1', 't/x', '', 'jo', 'jo', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO report (uuid, datamodule_id, name, macro_name, kind, body_sql, "
        "created_by, modified_by, created_at, modified_at) "
        "VALUES ('r1', 1, 'Sales', 'sales', 'query', 'SELECT 1', 'jo', 'jo', ?, ?)",
        (now, now),
    )
    conn.close()
    monkeypatch.setattr(db, "_migration_files", every)
    db.init_db_at(path)
    conn = apsw.Connection(path)
    rows = conn.execute("SELECT uuid, presentation, presentation_problem FROM report").fetchall()
    assert rows == [("r1", None, None)]
    conn.close()


def test_column_settings_have_one_default_each() -> None:
    """column_dict writes only what differs from ColumnSetting's defaults."""
    from strata.services.presentation import column_dict

    assert column_dict(ColumnSetting("A")) == {"name": "A"}
    full = ColumnSetting("A", "0", "right", "sum", True, "x", "y", True)
    assert column_dict(full) == {"name": "A"} | {
        f.name: getattr(full, f.name) for f in dataclasses.fields(full) if f.name != "name"
    }
