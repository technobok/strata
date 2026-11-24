"""Alerts: a datamodule's alert list hears when its data stops refreshing.

"Build failing" once per run of failures (never for the Build button: its
author sees the error), "recovered" on the next success, and "data is stale"
once when the last success is older than the stale alert.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from strata.models.datamodule import Datamodule
from tests.conftest import SALES_SCRIPT, Strata

BROKEN = "SELECT * FROM missing_table"


def _scheduled_build(strata: Strata, dm_uuid: str) -> Any:
    from strata.services import build_queue

    with strata.app.app_context():
        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        return build_queue.request_build(dm, "schedule:nightly", force=True)


def _subjects(mail: list[Any]) -> list[str]:
    return [m.subject for m in mail]


def _alerts_form(strata: Strata, dm_uuid: str, recipients: str = "", hours: str = "") -> Any:
    dm = strata.datamodule(dm_uuid)
    return strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={
            "path": dm.path,
            "build_script": dm.build_script,
            "ttl_seconds": "" if dm.ttl_seconds is None else str(dm.ttl_seconds),
            "alerts_present": "1",
            "alert_recipients": recipients,
            "stale_alert_hours": hours,
        },
    )


def test_a_failing_build_alerts_once_and_then_recovers(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    strata.set_script(dm_uuid, BROKEN)
    assert _scheduled_build(strata, dm_uuid).status == "failed"
    _scheduled_build(strata, dm_uuid)
    assert _subjects(mail) == ["[STRATA] test/sales: build failing"]
    [failing] = mail
    assert failing.to == ["root@example.com"]
    assert "missing_table" in failing.html and "previous version" in failing.html
    assert f'href="https://reports.example.com/strata/datamodules/{dm_uuid}/edit"' in failing.html

    strata.set_script(dm_uuid, SALES_SCRIPT)
    assert _scheduled_build(strata, dm_uuid).status == "done"
    assert _subjects(mail)[1] == "[STRATA] test/sales: recovered"
    assert "after 2 failed builds" in mail[1].html

    strata.set_script(dm_uuid, BROKEN)
    _scheduled_build(strata, dm_uuid)
    assert _subjects(mail)[2] == "[STRATA] test/sales: build failing"


def test_the_build_button_does_not_alert(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    strata.set_script(dm_uuid, BROKEN)
    strata.build(dm_uuid)
    assert strata.datamodule(dm_uuid).build_status == "failed" and mail == []
    _scheduled_build(strata, dm_uuid)  # the first failure nobody was watching
    assert _subjects(mail) == ["[STRATA] test/sales: build failing"]


def test_each_datamodule_has_its_own_alert_list(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    refused = _alerts_form(strata, dm_uuid, "jsmith\nnot a name!")
    assert refused.status_code == 200
    assert _alerts_form(strata, dm_uuid, "jsmith\nops@example.com").status_code == 302
    strata.set_script(dm_uuid, BROKEN)  # a post without the Alerts section keeps them
    assert strata.datamodule(dm_uuid).alert_recipients == ["jsmith", "ops@example.com"]
    _scheduled_build(strata, dm_uuid)
    assert mail[0].to == ["jsmith@example.com", "ops@example.com"]


def test_a_failing_gate_alerts(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE revenue > 200",
        name="Big",
        check="error",
        gate=True,
    )
    _scheduled_build(strata, dm_uuid)
    [alert] = mail
    assert "check &#39;Big&#39; found 1 row(s)" in alert.html
    assert "never been built" in alert.html


def test_a_failed_scheduled_run_goes_to_the_datamodule_alert_list(
    strata: Strata, mail: list[Any]
) -> None:
    from strata.services import schedule_service

    dm_uuid = strata.new_datamodule()
    rpt = strata.new_report(dm_uuid, body="SELECT cost FROM sales", name="Costs")
    strata.build(dm_uuid)
    _alerts_form(strata, dm_uuid, "@brs-finance")
    strata.client.post(
        f"/datamodules/reports/{rpt}/schedules/new",
        data={
            "name": "weekly",
            "kind": "every",
            "every_n": "15",
            "every_unit": "minutes",
            "timezone": "Pacific/Auckland",
            "enabled_present": "1",
            "enabled": "1",
            "recipients": "ops@example.com",
        },
    )
    with strata.app.app_context():
        schedule_service.run_due(datetime.now(UTC) + timedelta(hours=1))
    [sent] = mail
    assert sent.subject == "[FAILED] Costs — the scheduled run failed"
    assert sent.to == ["alice@example.com", "jsmith@example.com"]


def _age_live_version(strata: Strata, dm_uuid: str, hours: float) -> None:
    from strata.db import transaction

    when = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with strata.app.app_context(), transaction() as cur:
        cur.execute(
            "UPDATE datamodule_version SET build_ended_at = ? WHERE id = ?",
            (when, strata.datamodule(dm_uuid).live_version_id),
        )


def _sweep(strata: Strata) -> list[str]:
    from strata.services import alert_service

    with strata.app.app_context():
        return alert_service.sweep_stale()


def test_stale_data_alerts_once_until_the_next_success(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    assert _alerts_form(strata, dm_uuid, "@strata-admins", "1").status_code == 302
    assert strata.datamodule(dm_uuid).stale_alert_seconds == 3600
    _age_live_version(strata, dm_uuid, 0.5)
    assert _sweep(strata) == [] and mail == []  # within the hour

    _age_live_version(strata, dm_uuid, 2)
    assert _sweep(strata) == ["test/sales"]
    assert _sweep(strata) == []  # once
    assert _subjects(mail) == ["[STRATA] test/sales: data is stale"]
    assert "stale alert of 1 hour" in mail[0].html

    strata.build(dm_uuid)  # a success re-arms it
    assert strata.datamodule(dm_uuid).stale_alerted_at is None
    _age_live_version(strata, dm_uuid, 2)
    assert _sweep(strata) == ["test/sales"]


def test_without_a_stale_alert_nothing_is_swept(strata: Strata, mail: list[Any]) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    _age_live_version(strata, dm_uuid, 1000)
    assert _sweep(strata) == [] and mail == []


def test_an_alert_that_cannot_be_sent_never_changes_a_build(
    strata: Strata, mail: list[Any]
) -> None:
    def broken(_mail: Any) -> None:
        raise ConnectionError("outbox is down")

    strata.app.config["NOTIFY_SINK"] = broken
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    strata.set_script(dm_uuid, BROKEN)
    job = _scheduled_build(strata, dm_uuid)
    assert job.status == "failed" and "missing_table" in (job.error or "")
    assert strata.datamodule(dm_uuid).failure_alerted_at is None  # so the next failure tries again
    strata.set_script(dm_uuid, SALES_SCRIPT)
    assert _scheduled_build(strata, dm_uuid).status == "done"
