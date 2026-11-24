"""Report and check schedules: due runs are jobs, and their runs are mailed.

Rules (2026-11-21): a check that finds rows always mails its schedule's
recipients; a check with no rows sends nothing; a plain report always mails; a
failed query mails the admins only. Recipients and argument values belong to
each schedule. A run whose refresh fails still runs on the last good data and
says so in the mail's first line.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import apsw

from strata.models.build_job import BuildJob
from strata.models.report_run import ReportRun
from tests.conftest import PUBLIC_URL, Strata

NEGATIVE = "SELECT region, product, revenue FROM sales WHERE revenue < 0 ORDER BY product"
BAD_ROW_SCRIPT = """
CREATE TABLE sales AS
SELECT * FROM (VALUES
    ('North', 'A', 100.0, DATE '2026-01-05'),
    ('South', 'B', -5.0, DATE '2026-02-10')
) AS t(region, product, revenue, sold_on);
"""


def _schedule(strata: Strata, rpt: str, recipients: str, **params: str) -> Any:
    form = {
        "name": "weekly",
        "timezone": "Pacific/Auckland",
        "kind": "every",
        "every_n": "15",
        "every_unit": "minutes",
        "enabled_present": "1",
        "enabled": "1",
        "recipients": recipients,
        **{f"param_{k}": v for k, v in params.items()},
    }
    return strata.client.post(f"/datamodules/reports/{rpt}/schedules/new", data=form)


def _fire(strata: Strata) -> list[str]:
    from strata.services import schedule_service

    with strata.app.app_context():
        return schedule_service.run_due(datetime.now(UTC) + timedelta(hours=1))


def _runs(strata: Strata, rpt: str) -> list[ReportRun]:
    from strata.models.report import Report

    with strata.app.app_context():
        report = Report.get_by_uuid(rpt)
        assert report is not None
        return ReportRun.recent_for_report(report.id)


def test_a_check_with_rows_mails_its_recipients(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule(script=BAD_ROW_SCRIPT)
    rpt = strata.new_report(dm_uuid, body=NEGATIVE, name="Negative revenue", check="error")
    strata.build(dm_uuid)
    resp = _schedule(strata, rpt, "@brs-finance\njsmith\nops@example.com\nghost")
    assert resp.status_code == 302 and resp.headers["Location"].endswith(
        f"/reports/{rpt}/edit#schedules"
    )

    assert _fire(strata) == ["test/sales: Negative revenue: weekly"]
    [run] = _runs(strata, rpt)
    [sent] = mail
    assert sent.to == ["alice@example.com", "jsmith@example.com", "ops@example.com"]
    assert sent.subject == "[ERROR] Negative revenue — 1 row"
    assert "Data as of" in sent.html and "NZDT" in sent.html and "South" in sent.html
    assert f'href="{PUBLIC_URL}/runs/{run.uuid}"' in sent.html
    assert run.schedule_id is not None and run.notified_at is not None
    assert run.notify_note == (
        "mailed alice@example.com, jsmith@example.com, ops@example.com; not resolved: "
        "bob (in @brs-finance): disabled; carol (in @brs-finance): no email address; "
        "ghost: no such user"
    )
    page = strata.client.get(f"/runs/{run.uuid}").get_data(as_text=True)
    assert "mailed alice@example.com" in page and "(schedule <em>weekly</em>)" in page


def test_a_check_with_no_rows_sends_nothing(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid, body=NEGATIVE, name="Negative revenue", check="warning")
    strata.build(dm_uuid)
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [run] = _runs(strata, rpt)
    assert mail == [] and run.status == "empty" and run.notified_at is None
    assert run.notify_note == "no rows: a check that finds nothing sends no mail"


def test_a_plain_report_always_mails(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid, body=NEGATIVE, name="Refunds")
    strata.build(dm_uuid)
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [sent] = mail
    assert sent.subject == "Refunds — 0 rows"


def test_a_failed_query_mails_the_admins_only(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid, body="SELECT cost FROM sales", name="Costs")
    strata.build(dm_uuid)
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [sent] = mail
    assert sent.to == ["root@example.com"]
    assert sent.subject == "[FAILED] Costs — the scheduled run failed"
    [run] = _runs(strata, rpt)
    assert run.status == "error" and run.notify_note == (
        "query failed: mailed the alert list only (root@example.com)"
    )


def test_each_schedule_has_its_own_arguments(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE region = $region",
        name="By region",
        args=[{"name": "region", "type": "text", "widget": "input", "required": "yes"}],
    )
    strata.build(dm_uuid)
    refused = _schedule(strata, rpt, "jsmith")
    assert refused.status_code == 200
    assert "Argument &#39;region&#39; is required: set it on this schedule" in refused.get_data(
        as_text=True
    )

    assert _schedule(strata, rpt, "jsmith", region="South").status_code == 302
    _fire(strata)
    [run] = _runs(strata, rpt)
    assert run.args == {"region": ["South"]} and run.row_count == 1
    assert "South" in mail[0].html and "North" not in mail[0].html


def test_a_bad_recipient_line_is_refused(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    resp = _schedule(strata, rpt, "jsmith\nnot an address!")
    assert "&#39;not an address!&#39; is not a username, an @group or an email address" in (
        resp.get_data(as_text=True)
    )


def test_a_domain_username_is_a_recipient(strata: Strata, mail: list[Any]) -> None:
    """Gatekeeper's usernames carry their domain (asiap\\pawe)."""
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    assert _schedule(strata, rpt, "asiap\\jdoe").status_code == 302
    _fire(strata)
    assert [m.to for m in mail] == [["jdoe@example.com"]]


def test_a_failed_refresh_still_mails_on_the_last_good_data(
    strata: Strata, mail: list[Any]
) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={
            "path": "test/sales",
            "build_script": "SELECT * FROM missing_table",
            "ttl_seconds": "0",
        },
    )
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [run] = _runs(strata, rpt)
    alert, sent = mail  # the refresh's failure alerts the admins; then the run's mail
    assert (
        alert.to == ["root@example.com"] and alert.subject == "[STRATA] test/sales: build failing"
    )
    assert sent.to == ["jsmith@example.com"]
    assert run.row_count == 3 and (run.data_note or "").startswith("refresh failed:")
    first_line = sent.html.split("</p>")[0]
    assert (
        "Data as of" in first_line
        and "refresh failed:" in first_line
        and "missing_table" in first_line
    )


def test_a_mail_failure_is_recorded_and_the_run_stands(strata: Strata, mail: list[Any]) -> None:
    def broken(_mail: Any) -> None:
        raise ConnectionError("outbox is down")

    strata.app.config["NOTIFY_SINK"] = broken
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [run] = _runs(strata, rpt)
    assert run.status == "ok" and run.notify_note == "mail not sent: outbox is down"
    with strata.app.app_context():
        [job] = [
            j
            for j in BuildJob.recent_for_datamodule(strata.datamodule(dm_uuid).id)
            if j.kind == "run"
        ]
    assert job.status == "done" and job.run_id == run.id


def test_mail_goes_through_outbox(strata: Strata, mail: list[Any], tmp_path) -> None:
    from outbox.db import init_db_at as init_outbox_db

    del strata.app.config["NOTIFY_SINK"]
    outbox_db = tmp_path / "outbox.sqlite3"
    init_outbox_db(str(outbox_db))  # outbox owns its schema; Strata only queues into it
    strata.set_config({"outbox.db_path": str(outbox_db), "mail.sender": "strata@example.com"})
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid, name="Sales")
    strata.build(dm_uuid)
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    conn = apsw.Connection(str(outbox_db))
    rows = conn.execute(
        "SELECT from_address, to_recipients, subject, body_type, source_app FROM message"
    ).fetchall()
    conn.close()
    [(sender, to, subject, body_type, source)] = rows
    assert (sender, subject, body_type, source) == (
        "strata@example.com",
        "Sales — 3 rows",
        "html",
        "strata",
    )
    assert "jsmith@example.com" in json.dumps(to)


def test_without_outbox_the_run_says_why(strata: Strata, mail: list[Any]) -> None:
    del strata.app.config["NOTIFY_SINK"]
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    _schedule(strata, rpt, "jsmith")
    _fire(strata)
    [run] = _runs(strata, rpt)
    assert (run.notify_note or "").startswith("mail not sent: Outbox is not configured")


# ---------------------------------------------------------------------------
# Run jobs in the queue
# ---------------------------------------------------------------------------


def test_run_jobs_queue_apart_from_builds_and_one_runs_at_a_time(
    strata: Strata, mail: list[Any]
) -> None:
    from strata.worker import Worker

    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={
            "path": "test/sales",
            "build_script": strata.datamodule(dm_uuid).build_script,
            "ttl_seconds": "0",
        },
    )
    strata.app.config["WORKER_INLINE"] = False
    _schedule(strata, rpt, "jsmith")
    resp = strata.client.post(
        f"/datamodules/reports/{rpt}/schedules/new",
        data={
            "name": "second",
            "kind": "every",
            "every_n": "30",
            "every_unit": "minutes",
            "timezone": "Pacific/Auckland",
            "enabled_present": "1",
            "enabled": "1",
            "recipients": "ops@example.com",
        },
    )
    assert resp.status_code == 302
    assert len(_fire(strata)) == 2
    dm_id = strata.datamodule(dm_uuid).id
    with strata.app.app_context():
        from strata.services import build_queue

        BuildJob.enqueue(dm_id, "default", "manual:x", force=True)
        jobs = BuildJob.active_for_datamodule(dm_id)
        assert sorted(j.kind for j in jobs) == ["build", "run", "run"]  # a build never joins a run
        first = BuildJob.claim_next(["default"], "w1")
        assert first is not None and first.kind == "run"
        assert BuildJob.claim_next(["default"], "w2") is None  # one running job per datamodule
        build_queue.run_claimed(first)

    # One worker thread runs the rest, refreshing the stale datamodule inside the
    # run (TTL 0) without waiting on itself.
    worker = Worker(strata.app, ["default"], concurrency=1)
    assert worker.drain() == 2  # the other run, and the build
    runs = _runs(strata, rpt)
    assert len(runs) == 2 and all(r.status == "ok" and r.data_note is None for r in runs)
    assert sorted(m.to[0] for m in mail) == ["jsmith@example.com", "ops@example.com"]


def test_report_schedules_live_on_their_report(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid)
    _schedule(strata, rpt, "jsmith\n@brs-finance")
    report_page = strata.client.get(f"/datamodules/reports/{rpt}/edit").get_data(as_text=True)
    assert "weekly" in report_page and "<code>@brs-finance</code>" in report_page
    dm_page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert "No schedules: this datamodule is rebuilt only on demand" in dm_page


def test_the_admin_page_warns_until_the_public_url_is_set(strata: Strata) -> None:
    warning = "server.public_url</strong> is not set"
    assert warning in strata.client.get("/admin/").get_data(as_text=True)
    strata.set_config({"server.public_url": PUBLIC_URL})
    assert warning not in strata.client.get("/admin/").get_data(as_text=True)
