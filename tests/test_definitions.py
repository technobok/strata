"""Definition files (services/definitions.py, strata-admin import / export): one TOML
file per datamodule, matched by path; reports by name; schedules by name. What a
file lists is set, the rest is left alone unless pruned; a file is all or nothing;
a dry run refuses what a real run would and changes nothing; export imports back
unchanged.
"""

import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests.conftest import SALES_SCRIPT, FakeGatekeeper, Strata
from tests.test_connections import _new_connection

SALES_FILE = """
path = "test/sales"
description = "Sales by region"
tags = ["finance", "testrunner"]
ttl_seconds = 900
script = '''
{script}'''

[[report]]
name = "Big sales"
macro = "big_sales"
description = "Sales over the limit."
body = "SELECT * FROM sales WHERE revenue > $limit"
check = "error"

[[report.arg]]
name = "limit"
type = "integer"
required = false
default = "100"

[[report.schedule]]
name = "workdaily"
kind = "daily"
fields = {{ times = "17:00", days = ["MO", "TU", "WE", "TH", "FR"] }}
timezone = "Australia/Brisbane"
recipients = ['jsmith', 'asiap\\jdoe', 'ghost']
""".replace("{script}", SALES_SCRIPT.replace("{", "{{").replace("}", "}}")).format()


def _import(strata: Strata, *texts: str, dry_run: bool = False, prune: bool = False) -> list[Any]:
    from strata.services import definitions

    with strata.app.app_context():
        defs = [definitions.parse(t, f"file{i}") for i, t in enumerate(texts)]
        return definitions.import_definitions(defs, "importer", dry_run=dry_run, prune=prune)


def _state(strata: Strata, path: str) -> dict[str, Any] | None:
    """What import controls of a datamodule, read back from the database."""
    with strata.app.app_context():
        from strata.models.datamodule import Datamodule
        from strata.models.report import Report
        from strata.models.report_arg import ReportArg
        from strata.models.report_check import ReportCheck
        from strata.models.schedule import Schedule
        from strata.models.tag import Tag

        dm = Datamodule.get_by_path(path)
        if dm is None:
            return None
        reports = {}
        for r in Report.get_for_datamodule(dm.id):
            check = ReportCheck.get(r.id)
            reports[r.name] = {
                "macro": r.macro_name,
                "body": r.body_sql,
                "check": (check.severity, check.gate) if check else None,
                "args": [
                    (a.name, a.data_type, a.default_value) for a in ReportArg.get_for_report(r.id)
                ],
                "schedules": {
                    s.name: (s.enabled, s.recipients, s.next_run_at is not None)
                    for s in Schedule.for_report(r.id)
                },
            }
        return {
            "uuid": dm.uuid,
            "description": dm.description,
            "ttl": dm.ttl_seconds,
            "tags": [t.name for t in Tag.for_datamodule(dm.id)],
            "alerts": dm.alert_recipients,
            "stale": dm.valid_until is not None,
            "reports": reports,
        }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_a_file_makes_a_datamodule_its_check_and_schedule(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    (out,) = _import(strata, SALES_FILE)
    assert out.action == "create" and out.refused is None
    assert out.notes == [
        "Schedule 'workdaily' of report 'Big sales': left out recipients Gatekeeper does not "
        "know: ghost"
    ]
    state = _state(strata, "test/sales")
    assert state is not None
    assert state["description"] == "Sales by region" and state["ttl"] == 900
    assert state["tags"] == ["finance", "testrunner"]  # made by the import
    assert state["alerts"] == ["@strata-admins"]  # as the form does
    big = state["reports"]["Big sales"]
    assert big["macro"] == "big_sales" and big["check"] == ("error", False)
    assert big["args"] == [("limit", "integer", "100")]
    assert big["schedules"] == {"workdaily": (True, ["jsmith", "asiap\\jdoe"], True)}

    strata.build(state["uuid"])  # it builds, and the check runs on its data
    page = strata.client.get(f"/datamodules/{state['uuid']}/edit").get_data(as_text=True)
    assert "Big sales" in page and "excel-link" in page


def test_the_same_file_again_changes_nothing(strata: Strata, gatekeeper: FakeGatekeeper) -> None:
    _import(strata, SALES_FILE)
    (again,) = _import(strata, SALES_FILE)
    assert (again.action, again.changes) == ("unchanged", [])


def test_a_refused_file_changes_nothing_and_the_next_file_is_still_tried(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    bad = SALES_FILE.replace(
        'body = "SELECT * FROM sales WHERE revenue > $limit"', 'body = "DELETE FROM sales"'
    )
    other = 'path = "test/other"\nscript = "CREATE TABLE t AS SELECT 1 AS x"\n'
    refused, made = _import(strata, bad, other)
    assert refused.action == "refused" and "Report 'Big sales'" in (refused.refused or "")
    assert _state(strata, "test/sales") is None  # not even the datamodule
    with strata.app.app_context():
        from strata.models.tag import Tag

        assert Tag.get_by_name("finance") is None  # nor its tags
    assert made.action == "create" and _state(strata, "test/other") is not None


def test_a_dry_run_changes_nothing_and_refuses_what_a_real_run_would(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    (would,) = _import(strata, SALES_FILE, dry_run=True)
    assert would.action == "create" and would.notes  # the unknown recipient, as a real run
    assert _state(strata, "test/sales") is None
    unknown_owner = SALES_FILE.replace("ttl_seconds = 900", 'ttl_seconds = 900\nowners = ["ghost"]')
    (refused,) = _import(strata, unknown_owner, dry_run=True)
    assert refused.refused == "Access: Gatekeeper has no user or group ghost."


def test_what_a_file_leaves_out_stays_unless_pruned(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    _import(strata, SALES_FILE)
    state = _state(strata, "test/sales")
    assert state is not None
    strata.new_report(state["uuid"], name="By region", body="SELECT region FROM sales")
    (kept,) = _import(strata, SALES_FILE)
    assert kept.action == "unchanged"
    assert set(_state(strata, "test/sales")["reports"]) == {"Big sales", "By region"}  # type: ignore[index]
    (pruned,) = _import(strata, SALES_FILE, prune=True)
    assert pruned.changes == ["report 'By region' removed"]
    assert set(_state(strata, "test/sales")["reports"]) == {"Big sales"}  # type: ignore[index]


def test_a_changed_script_makes_built_data_stale_and_reports_follow_the_new_script(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    _import(strata, SALES_FILE)
    uuid = _state(strata, "test/sales")["uuid"]  # type: ignore[index]
    strata.build(uuid)
    # The new script makes a column the live data does not have yet: the report on it
    # is not checked against the old data.
    new = SALES_FILE.replace(
        "CREATE TABLE sales AS", "CREATE TABLE sales AS SELECT *, 1 AS flag FROM ("
    ).replace("revenue > $limit", "revenue > $limit AND flag = 1")
    new = new.replace(";\n'''", ");\n'''", 1)
    (out,) = _import(strata, new)
    assert out.refused is None, out.refused
    assert out.changes == ["script", "report 'Big sales'"]
    assert "its script changed: its data is stale until the next build" in out.notes
    assert _state(strata, "test/sales")["stale"]  # type: ignore[index]


def test_a_report_keeps_its_macro_name(strata: Strata, gatekeeper: FakeGatekeeper) -> None:
    _import(strata, SALES_FILE)
    (out,) = _import(strata, SALES_FILE.replace('macro = "big_sales"', 'macro = "sales_big"'))
    assert out.refused is not None and "keeps its macro name 'big_sales'" in out.refused


def test_a_dependency_on_a_datamodule_made_in_the_same_run_binds_in_order(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    child = """
path = "test/child"
script = "CREATE TABLE c AS SELECT * FROM parent.t"
[dependencies]
parent = "test/parent"
"""
    parent = 'path = "test/parent"\nscript = "CREATE TABLE t AS SELECT 1 AS x"\n'
    would = _import(strata, child, parent, dry_run=True)
    assert [o.path for o in would] == ["test/parent", "test/child"]  # parents first
    assert "made by an earlier file of this run" in would[1].notes[0]
    made = _import(strata, child, parent)
    assert [(o.path, o.action) for o in made] == [
        ("test/parent", "create"),
        ("test/child", "create"),
    ]
    strata.build(_state(strata, "test/child")["uuid"])  # type: ignore[index]


def test_connections_bind_by_name(
    strata: Strata, gatekeeper: FakeGatekeeper, tmp_path: Path
) -> None:
    _new_connection(strata, "warehouse", "sqlite", path=str(tmp_path / "w.sqlite3"))
    text = 'path = "test/wh"\nscript = "CREATE TABLE t AS SELECT 1 AS x"\n[connections]\nwh = "warehouse"\n'
    (out,) = _import(strata, text)
    assert out.action == "create" and out.changes == ["connection wh"]
    (missing,) = _import(strata, text.replace('"warehouse"', '"nosuch"'))
    assert missing.refused == "Connection 'nosuch' (as 'wh'): there is none."


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ('path = "a/b"\nttl = 5\n', "unknown key(s) ttl"),
        (
            'path = "a/b"\nttl_seconds = "soon"\n',
            "'ttl_seconds' must be a whole number or \"never\"",
        ),
        ('path = "a/b"\n[[report]]\nname = "x"\n', "report 'x': 'body' is required"),
        (
            'path = "a/b"\n[[report]]\nname = "x"\nbody = "SELECT 1"\nseverity = "error"\n',
            "unknown key(s) severity",
        ),
        ('description = "no path"\n', "'path' is required"),
        ("path = 'a/b'\nowners = 'jsmith'\n", "'owners' must be a list of text"),
        ("path = [", "not a TOML file"),
    ],
)
def test_a_file_of_the_wrong_shape_is_refused_when_read(text: str, words: str) -> None:
    from strata.services import definitions

    with pytest.raises(definitions.DefinitionError) as exc:
        definitions.parse(text, "f.toml")
    assert words in str(exc.value)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        "asiap\\pawe",
        "it's",
        'say "hi"',
        "SELECT 1\nFROM t\n",
        "ends with a quote'",
        "line\r\nbreaks",
        "has ''' inside\n",
        "tab\there",
        "bell\x07",
        "del\x7f",
        "Ünïcode — ok",
        "",
    ],
)
def test_toml_strings_read_back_as_written(text: str) -> None:
    from strata.services.definitions import toml_string

    assert tomllib.loads(f"v = {toml_string(text)}")["v"] == text


def test_export_imports_back_unchanged(
    strata: Strata, gatekeeper: FakeGatekeeper, tmp_path: Path
) -> None:
    from strata.services import definitions

    _import(strata, SALES_FILE)
    uuid = _state(strata, "test/sales")["uuid"]  # type: ignore[index]
    # Things made in the forms too: a plain report, a rebuild schedule, a dependency.
    strata.new_report(
        uuid, name="By region", body="SELECT region, sum(revenue) AS r FROM sales GROUP BY 1"
    )
    parent = 'path = "test/parent"\nscript = "CREATE TABLE t AS SELECT 1 AS x"\n'
    _import(strata, parent)
    resp = strata.client.post(
        f"/datamodules/{uuid}/schedules/new",
        data={"name": "nightly", "kind": "daily", "times": "02:00", "timezone": "UTC"},
    )
    assert resp.status_code == 302, strata.flashes()
    with strata.app.app_context():
        from strata.models.datamodule import Datamodule

        dm = Datamodule.get_by_path("test/sales")
        assert dm is not None
        parent_dm = Datamodule.get_by_path("test/parent")
        assert parent_dm is not None
        parent_uuid = parent_dm.uuid
    resp = strata.client.post(
        f"/datamodules/{uuid}/dependencies",
        data={"depends_on_uuid": parent_uuid, "local_name": "p"},
    )
    assert resp.status_code == 302

    with strata.app.app_context():
        dm = Datamodule.get_by_path("test/sales")
        assert dm is not None
        text = definitions.export_datamodule(dm)
    assert "[dependencies]" in text and "p = 'test/parent'" in text
    assert "[[schedule]]" in text and "name = 'nightly'" in text
    (again,) = _import(strata, text)
    assert (again.action, again.changes, again.notes) == ("unchanged", [], [])


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def test_the_commands_import_and_export_files(strata: Strata, tmp_path: Path) -> None:
    from strata.cli import main

    src = tmp_path / "defs"
    (src / "test").mkdir(parents=True)
    (src / "test" / "cli.toml").write_text(
        'path = "test/cli"\nscript = "CREATE TABLE t AS SELECT 1 AS x"\n'
        '[[report]]\nname = "All"\nbody = "SELECT * FROM t"\n'
    )
    runner = CliRunner()
    dry = runner.invoke(main, ["import", "--dry-run", str(src)])
    assert dry.exit_code == 0, dry.output
    assert "create test/cli" in dry.output and "(dry run: nothing was changed)" in dry.output
    assert _state(strata, "test/cli") is None

    made = runner.invoke(main, ["import", "--as", "asiap\\pawe", str(src)])
    assert made.exit_code == 0, made.output
    assert "import: 1 created, 0 updated, 0 unchanged, 0 refused" in made.output
    assert _state(strata, "test/cli") is not None

    out = tmp_path / "out"
    exported = runner.invoke(main, ["export", "--out", str(out), "--only", "test/*"])
    assert exported.exit_code == 0, exported.output
    assert (out / "test" / "cli.toml").exists()
    again = runner.invoke(main, ["import", str(out)])
    assert again.exit_code == 0 and "unchanged test/cli" in again.output

    (src / "test" / "bad.toml").write_text('path = "test/bad"\n[[report]]\nname = "x"\n')
    refused = runner.invoke(main, ["import", str(src)])
    assert refused.exit_code == 1
    assert "REFUSED" in refused.output and "'body' is required" in refused.output
