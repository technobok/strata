"""Checks (reports with exception settings), gate checks inside builds, and runs.

A run saves its rows as a Parquet snapshot; the run page and downloads read it
back. Gate checks run on the new data before the swap: rows fail the build and
the previous version keeps serving.
"""

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta

from strata.models.report_run import ReportRun
from tests.conftest import Strata

NEGATIVE = "SELECT region, product, revenue FROM sales WHERE revenue < 0 ORDER BY product"

BAD_ROW_SCRIPT = """
CREATE TABLE sales AS
SELECT * FROM (VALUES
    ('North', 'A', 100.0, DATE '2026-01-05'),
    ('South', 'B', -5.0, DATE '2026-02-10')
) AS t(region, product, revenue, sold_on);
"""


def _rows(strata: Strata, rpt: str, query: str = "") -> list[dict]:
    resp = strata.client.get(f"/r/{rpt}.json{query}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return json.loads(resp.get_data(as_text=True))


def _run_now(strata: Strata, rpt: str, **form: str) -> ReportRun:
    resp = strata.client.post(f"/reports/{rpt}/run", data=form)
    assert resp.status_code == 302, resp.get_data(as_text=True)
    match = re.search(r"/runs/([0-9a-f-]{36})$", resp.headers["Location"])
    assert match, resp.headers["Location"]
    with strata.app.app_context():
        run = ReportRun.get_by_uuid(match.group(1))
    assert run is not None
    return run


def _snapshot(strata: Strata, dm_uuid: str, run: ReportRun):
    from strata.services import run_service

    with strata.app.app_context():
        return run_service.snapshot_file(dm_uuid, run)


# ---------------------------------------------------------------------------
# Check settings
# ---------------------------------------------------------------------------


def test_a_report_becomes_a_check_and_back(strata: Strata) -> None:
    from strata.models.report import Report
    from strata.models.report_check import ReportCheck

    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid, body=NEGATIVE, name="Negative revenue", check="error")
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert '<span class="severity severity-error">error</span>' in page

    with strata.app.app_context():
        report = Report.get_by_uuid(rpt)
        assert report is not None
        assert ReportCheck.get(report.id) == ReportCheck(report.id, "error", False)
    strata.client.post(
        f"/datamodules/reports/{rpt}/edit",
        data={"name": "Negative revenue", "body_sql": NEGATIVE},
    )
    with strata.app.app_context():
        assert ReportCheck.get(report.id) is None


def test_a_gate_needs_defaults_for_required_arguments(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/reports/new",
        data={
            "name": "Below",
            "body_sql": "SELECT * FROM sales WHERE revenue < $floor",
            "arg_name": ["floor"],
            "arg_type": ["number"],
            "arg_widget": ["input"],
            "arg_required": ["yes"],
            "arg_default": [""],
            "arg_constraint": [""],
            "arg_description": [""],
            "is_check": "on",
            "severity": "warning",
            "gate": "on",
        },
    )
    assert resp.status_code == 200  # the form again, with the reason
    assert "give &#39;floor&#39; a default" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Gate checks inside builds
# ---------------------------------------------------------------------------


def test_a_gate_with_rows_fails_the_build_and_the_old_version_serves(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    sales = strata.new_report(dm_uuid)
    strata.new_report(dm_uuid, body=NEGATIVE, name="Negative revenue", check="error", gate=True)
    strata.build(dm_uuid)
    good = strata.datamodule(dm_uuid)
    assert good.build_status == "ready"

    strata.set_script(dm_uuid, BAD_ROW_SCRIPT)
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert dm.last_build_error == "check 'Negative revenue' found 1 row(s): South, B, -5.0"
    assert dm.live_version_id == good.live_version_id
    assert len(_rows(strata, sales)) == 3  # the last good data


def test_a_gate_that_cannot_run_fails_the_build(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE cost > 0",
        name="Cost",
        check="warning",
        gate=True,
    )
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert (dm.last_build_error or "").startswith("check 'Cost' could not run:")


def test_a_check_that_is_not_a_gate_never_touches_builds(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(script=BAD_ROW_SCRIPT)
    strata.new_report(dm_uuid, body=NEGATIVE, name="Negative revenue", check="error")
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "ready"


def test_a_gate_runs_with_its_argument_defaults(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE revenue < $floor",
        name="Below floor",
        args=[
            {
                "name": "floor",
                "type": "number",
                "widget": "input",
                "default": "80",
                "required": "yes",
            }
        ],
        check="warning",
        gate=True,
    )
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).last_build_error == (
        "check 'Below floor' found 1 row(s): North, C, 75.25, 2026-03-15"
    )


def test_a_gate_that_outlasts_the_build_limit_reports_the_limit(strata: Strata) -> None:
    strata.set_config({"build.timeout_seconds": 1})
    dm_uuid = strata.new_datamodule()
    strata.new_report(
        dm_uuid,
        body="SELECT x FROM range(100000000000) r(x) WHERE x % 7 = 3 AND x < 0",
        name="Slow",
        check="warning",
        gate=True,
    )
    strata.build(dm_uuid)
    assert "time limit of 1s" in (strata.datamodule(dm_uuid).last_build_error or "")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_run_now_saves_the_rows_and_opens_the_run(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    run = _run_now(strata, rpt)
    assert (run.status, run.row_count, run.triggered_by, run.data_note) == ("ok", 3, "tester", None)
    assert run.data_as_of is not None
    assert _snapshot(strata, dm_uuid, run).exists()

    page = strata.client.get(f"/runs/{run.uuid}").get_data(as_text=True)
    assert "3 rows" in page and "South" in page
    assert f'href="/r/{rpt}.html"' in page  # View current
    filtered = strata.client.get(f"/runs/{run.uuid}?region__eq=North").get_data(as_text=True)
    assert "2 rows" in filtered
    csv = strata.client.get(f"/runs/{run.uuid}.csv?sort=-revenue").get_data(as_text=True)
    assert csv.splitlines()[1].startswith("South,B,250.5")
    edit = strata.client.get(f"/datamodules/reports/{rpt}/edit").get_data(as_text=True)
    assert f"/runs/{run.uuid}" in edit  # the report lists its runs


def test_a_run_keeps_its_rows_when_the_data_changes(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    run = _run_now(strata, rpt)
    strata.set_script(dm_uuid, BAD_ROW_SCRIPT)
    strata.build(dm_uuid)
    assert len(_rows(strata, rpt)) == 2
    assert "3 rows" in strata.client.get(f"/runs/{run.uuid}").get_data(as_text=True)


def test_a_saved_run_records_its_arguments(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE region = $region",
        args=[{"name": "region", "type": "text", "widget": "input", "required": "yes"}],
    )
    strata.build(dm_uuid)
    run = _run_now(strata, rpt, region="North", not_an_arg="x")
    assert (run.args, run.row_count) == ({"region": ["North"]}, 2)
    page = strata.client.get(f"/runs/{run.uuid}").get_data(as_text=True)
    assert f'href="/r/{rpt}.html?region=North"' in page


def test_empty_and_failed_runs(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    neg = strata.new_report(dm_uuid, body=NEGATIVE, name="Negative revenue", check="error")
    needs = strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE region = $region",
        name="By region",
        args=[{"name": "region", "type": "text", "widget": "input", "required": "yes"}],
    )
    strata.build(dm_uuid)
    empty = _run_now(strata, neg)
    assert (empty.status, empty.row_count) == ("empty", 0)
    failed = _run_now(strata, needs)
    assert failed.status == "error" and failed.snapshot_path is None
    assert failed.error == "Argument 'region' is required"
    assert "Argument &#39;region&#39; is required" in strata.client.get(
        f"/runs/{failed.uuid}"
    ).get_data(as_text=True)


def test_runs_need_a_login_and_run_now_needs_an_owner(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    run = _run_now(strata, rpt)
    assert strata.client.get(f"/runs/{run.uuid}", headers={"X-Anon": "1"}).status_code == 302
    assert strata.client.get(f"/runs/{run.uuid}", headers={"X-User": "viewer"}).status_code == 200
    resp = strata.client.post(f"/reports/{rpt}/run", headers={"X-User": "viewer"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Out-of-date data: a run says so
# ---------------------------------------------------------------------------


def _stale_and_failing(strata: Strata) -> tuple[str, str]:
    """A built datamodule whose next rebuild fails, with TTL 0 (always stale)."""
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={
            "path": "test/sales",
            "build_script": "SELECT * FROM missing_table",
            "ttl_seconds": "0",
        },
    )
    assert resp.status_code == 302
    return dm_uuid, rpt


def test_run_now_on_data_that_failed_to_refresh_says_so(strata: Strata) -> None:
    _dm_uuid, rpt = _stale_and_failing(strata)
    run = _run_now(strata, rpt)
    assert run.row_count == 3  # the last good version
    assert (run.data_note or "").startswith("refresh failed:")
    page = strata.client.get(f"/runs/{run.uuid}").get_data(as_text=True)
    assert "This run may show out-of-date data: refresh failed:" in page


def test_a_worker_run_falls_back_to_the_last_good_version(strata: Strata) -> None:
    from strata.services import report_service, run_service

    _dm_uuid, rpt = _stale_and_failing(strata)
    with strata.app.app_context():
        report, dm = report_service.resolve(rpt) or (None, None)
        assert report is not None and dm is not None
        version, note = run_service.version_inline(report, dm)
        assert version is not None and version.id == dm.live_version_id
        assert (note or "").startswith("refresh failed:") and "missing_table" in note
        run = run_service.run(
            report, dm, {}, version=version, data_note=note, triggered_by="schedule:x"
        )
    assert (run.status, run.row_count) == ("ok", 3)


# ---------------------------------------------------------------------------
# Retention and clean-up
# ---------------------------------------------------------------------------


def test_expired_runs_are_deleted_with_their_files(strata: Strata) -> None:
    from strata.db import transaction
    from strata.services import run_service

    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    old, new = _run_now(strata, rpt), _run_now(strata, rpt)
    stamp = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    with strata.app.app_context():
        with transaction() as cur:
            cur.execute("UPDATE report_run SET started_at = ? WHERE id = ?", (stamp, old.id))
        assert run_service.prune_runs() == [f"{old.uuid[:8]} ({stamp[:10]})"]
        assert ReportRun.get_by_uuid(old.uuid) is None
        assert ReportRun.get_by_uuid(new.uuid) is not None
    assert not _snapshot(strata, dm_uuid, old).exists()
    assert _snapshot(strata, dm_uuid, new).exists()


def test_reconcile_removes_old_orphan_snapshots_only(strata: Strata) -> None:
    from strata.services import datamodule_service

    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    kept = _run_now(strata, rpt)
    runs_dir = _snapshot(strata, dm_uuid, kept).parent
    old_orphan, new_orphan = runs_dir / "old.parquet", runs_dir / "new.parquet"
    old_orphan.write_bytes(b"x")
    new_orphan.write_bytes(b"x")
    two_hours_ago = time.time() - 7200
    os.utime(old_orphan, (two_hours_ago, two_hours_ago))
    with strata.app.app_context():
        rep = datamodule_service.reconcile()
    assert rep.orphan_snapshots == [str(old_orphan)]
    assert not old_orphan.exists() and new_orphan.exists()
    assert _snapshot(strata, dm_uuid, kept).exists()


def test_deleting_a_report_deletes_its_runs(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    run = _run_now(strata, rpt)
    snapshot = _snapshot(strata, dm_uuid, run)
    strata.client.post(f"/datamodules/reports/{rpt}/delete")
    with strata.app.app_context():
        assert ReportRun.get_by_uuid(run.uuid) is None
    assert not snapshot.exists()
