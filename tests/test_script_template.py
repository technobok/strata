"""Jinja build scripts: a loop over a dependency's rows renders one branch per row.

The rendered text is what runs and what the version keeps. Errors name the
template line; the sandbox refuses attribute tricks; a script without
``{{``/``{%`` is untouched.
"""

import json

from strata.services import script_template
from tests.conftest import Strata

COMPANIES = """
CREATE TABLE companies AS
SELECT * FROM (VALUES ('BRS', 'brs_db'), ('OTA', 'ota_db'), ('O''Neil', 'on_db'))
    AS t(shortname, companynamesql);
"""

LOOP = """CREATE TABLE stock AS
{% for c in rows('companies', "SELECT shortname, companynamesql FROM companies ORDER BY shortname") %}
{% if not loop.first %}UNION ALL{% endif %}
SELECT {{ c.shortname | sqlstr }} AS company, {{ c.companynamesql | sqlstr }} AS db
{% endfor %}
"""


def _with_companies(strata: Strata, script: str) -> str:
    companies = strata.new_datamodule(path="ref/companies", script=COMPANIES)
    strata.build(companies)
    dm_uuid = strata.new_datamodule(path="ops/stock", script=script)
    strata.add_dependency(dm_uuid, companies, "companies")
    return dm_uuid


def test_a_loop_renders_one_branch_per_row_and_the_version_keeps_it(strata: Strata) -> None:
    dm_uuid = _with_companies(strata, LOOP)
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "ready", strata.datamodule(
        dm_uuid
    ).last_build_error
    rpt = strata.new_report(
        dm_uuid, body="SELECT * FROM stock ORDER BY company", name="Stock", macro="stock"
    )
    rows = json.loads(strata.client.get(f"/r/{rpt}.json").get_data(as_text=True))
    assert rows == [
        {"company": "BRS", "db": "brs_db"},
        {"company": "O'Neil", "db": "on_db"},
        {"company": "OTA", "db": "ota_db"},
    ]
    [version] = strata.versions(dm_uuid)
    snapshot = version.build_script_snapshot or ""
    assert "{%" not in snapshot and snapshot.count("UNION ALL") == 2
    assert "SELECT 'O''Neil' AS company" in snapshot


def test_preview_shows_the_rendered_script(strata: Strata) -> None:
    dm_uuid = _with_companies(strata, LOOP)
    resp = strata.client.post(f"/datamodules/{dm_uuid}/preview-script", data={"build_script": LOOP})
    page = resp.get_data(as_text=True)
    assert "SELECT &#39;BRS&#39; AS company" in page and page.count("UNION ALL") == 2
    plain = strata.client.post(
        f"/datamodules/{dm_uuid}/preview-script", data={"build_script": "SELECT 1"}
    ).get_data(as_text=True)
    assert "it runs as written" in plain


def test_template_errors_name_the_line(strata: Strata) -> None:
    dm_uuid = _with_companies(strata, "SELECT 1;\n-- two\n{{ company }}\n")
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).last_build_error == (
        "build script, line 3: 'company' is undefined"
    )
    strata.set_script(dm_uuid, "SELECT 1;\n{% for c in rows('companies', 'SELECT 1') %}\n")
    strata.build(dm_uuid)
    assert (strata.datamodule(dm_uuid).last_build_error or "").startswith(
        "build script, line 2: Unexpected end of template"
    )


def test_rows_needs_a_dependency_and_a_single_select(strata: Strata) -> None:
    dm_uuid = _with_companies(strata, "{% for c in rows('nope', 'SELECT 1') %}{% endfor %}")
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).last_build_error == (
        "build script, line 1: rows('nope', ...): no dependency of that name "
        "(dependencies: companies)"
    )
    strata.set_script(
        dm_uuid, "{% for c in rows('companies', 'DROP TABLE companies') %}{% endfor %}"
    )
    strata.build(dm_uuid)
    assert (strata.datamodule(dm_uuid).last_build_error or "").endswith(
        "The query in rows('companies', ...) must be a SELECT statement."
    )


def test_the_sandbox_refuses_attribute_tricks(strata: Strata) -> None:
    dm_uuid = _with_companies(strata, "{{ ''.__class__.__mro__ }}")
    strata.build(dm_uuid)
    assert "unsafe" in (strata.datamodule(dm_uuid).last_build_error or "")


def test_a_plain_script_is_untouched() -> None:
    script = "CREATE TABLE t AS SELECT '{ not a template }' AS x, '{' AS y"
    assert script_template.render(script, {}) is script


def test_the_filters_quote() -> None:
    assert script_template.sqlstr("O'Neil") == "'O''Neil'"
    assert script_template.sqlstr(None) == "NULL"
    assert script_template.ident('net "sales"') == '"net ""sales"""'


def test_rows_is_capped(strata: Strata, monkeypatch) -> None:
    monkeypatch.setattr(script_template, "MAX_ROWS", 2)
    dm_uuid = _with_companies(strata, LOOP)
    strata.build(dm_uuid)
    assert (strata.datamodule(dm_uuid).last_build_error or "").endswith(
        "rows('companies', ...) returned more than 2 rows"
    )
