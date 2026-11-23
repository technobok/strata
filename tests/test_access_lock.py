"""The dependency rule and the access lock (AC4).

Each owner of a dependant must be able to read each parent (be one of its owners
or viewers). Every build checks it, with Gatekeeper's groups as they are now. If
an owner lost access, or Gatekeeper cannot say, the build fails and the datamodule
is LOCKED: it serves nothing to anyone (pages, data, runs, admin keys, Excel links)
until its access is fixed and a build succeeds. Its owners and the admins still
open its page to fix it. A dependant of a locked datamodule fails to build but is
not itself locked.
"""

from typing import Any

import pytest

from tests.conftest import FakeGatekeeper, Strata, as_user
from tests.test_excel_links import _box, _links, _local
from tests.test_notify import _fire, _runs, _schedule

ALICE = as_user("alice")
VIEWER = as_user("vera")
ANON = {"X-Anon": "1"}
PARENT_SCRIPT = "CREATE TABLE salaries AS SELECT * FROM (VALUES (100), (250)) t(amount)"
CHILD_SCRIPT = "CREATE TABLE totals AS SELECT sum(amount) AS total FROM pay.salaries"


@pytest.fixture
def payroll(strata: Strata, gatekeeper: FakeGatekeeper) -> dict[str, Any]:
    """finance/payroll (pam owns; @payroll-readers view) and finance/summary, owned
    by alice (a payroll reader) and viewed by everyone, reading it as `pay`."""
    gatekeeper.users["pam"] = gatekeeper.users["alice"].__class__("pam", "pam@example.com")
    gatekeeper.users["vera"] = gatekeeper.users["alice"].__class__("vera", "vera@example.com")
    gatekeeper.groups["payroll-readers"] = ["alice", "pam"]
    parent = strata.new_datamodule(path="finance/payroll", script=PARENT_SCRIPT)
    assert strata.set_access(parent, owners="pam", viewers="@payroll-readers").status_code == 302
    child = strata.new_datamodule(path="finance/summary", script=CHILD_SCRIPT)
    assert strata.set_access(child, owners="alice", viewers=None).status_code == 302
    strata.add_dependency(child, parent, "pay")
    rpt = strata.new_report(child, body="SELECT * FROM totals", name="Totals", macro="totals")
    strata.build(child)
    assert strata.datamodule(child).build_status == "ready"
    return {"gk": gatekeeper, "parent": parent, "child": child, "report": rpt}


def _lose_access(payroll: dict[str, Any]) -> None:
    payroll["gk"].groups["payroll-readers"].remove("alice")


def _build(strata: Strata, dm_uuid: str) -> None:
    """Build as the admin, and drop the "Build failed" flash: the test client shares one
    session between the people these tests play."""
    strata.build(dm_uuid)
    with strata.client.session_transaction() as sess:
        sess.pop("_flashes", None)


def _locked(strata: Strata, dm_uuid: str) -> str | None:
    return strata.datamodule(dm_uuid).access_lock


REASON = "alice may not read 'finance/payroll' (dependency pay)"


def test_a_build_whose_owner_lost_a_parent_fails_and_locks(
    strata: Strata, payroll: dict[str, Any], mail: list[Any]
) -> None:
    _lose_access(payroll)
    _build(strata, payroll["child"])
    dm = strata.datamodule(payroll["child"])
    assert (dm.build_status, dm.access_lock, dm.last_build_error) == ("failed", REASON, REASON)
    # Mailed once although the Build button started it; a second failure is quiet.
    assert [m.subject for m in mail] == ["[STRATA] finance/summary: locked (access)"]
    assert REASON.replace("'", "&#39;") in mail[0].html
    _build(strata, payroll["child"])
    assert len(mail) == 1


def test_a_locked_datamodule_serves_nothing_to_anyone(
    strata: Strata, payroll: dict[str, Any], mail: list[Any]
) -> None:
    rpt, child = payroll["report"], payroll["child"]
    excel = _local(_links(_box(strata, rpt, headers=VIEWER))["CSV"])
    admin_key = strata.new_api_key(child)
    run = strata.client.post(f"/reports/{rpt}/run").headers["Location"].rsplit("/", 1)[-1]
    _lose_access(payroll)
    _build(strata, child)

    viewer_page = strata.client.get(f"/r/{rpt}.html", headers=VIEWER)
    assert viewer_page.status_code == 403
    text = viewer_page.get_data(as_text=True)
    assert "This datamodule is locked" in text and "alice may not" not in text
    for headers in (ALICE, {}):  # its owner and an admin: refused, but told why
        page = strata.client.get(f"/r/{rpt}.html", headers=headers)
        assert page.status_code == 403 and "alice may not read" in page.get_data(as_text=True)
    for url, headers in (
        (f"/r/{rpt}.csv", VIEWER),
        (f"/r/{rpt}.csv", {}),
        (excel, ANON),
        (f"/r/{rpt}.csv?key={admin_key}", ANON),
        (f"/r/{rpt}/excel", VIEWER),
        (f"/runs/{run}", {}),
        (f"/runs/{run}.csv", VIEWER),
    ):
        resp = strata.client.get(url, headers=headers)
        assert resp.status_code == 403, url
        assert "250" not in resp.get_data(as_text=True)
    assert strata.client.post(f"/reports/{rpt}/run").status_code == 403
    assert strata.client.post(f"/r/{rpt}/build", headers=VIEWER).status_code == 403
    # Its page stays open to its owners and the admins, to fix it.
    page = strata.client.get(f"/datamodules/{child}/edit", headers=ALICE).get_data(as_text=True)
    assert 'class="error-box access-lock"' in page
    assert "alice may not read" in page.replace("&#39;", "'")


def test_fixing_access_and_a_successful_build_unlock_it(
    strata: Strata, payroll: dict[str, Any], mail: list[Any]
) -> None:
    _lose_access(payroll)
    _build(strata, payroll["child"])
    payroll["gk"].groups["payroll-readers"].append("alice")  # access restored
    _build(strata, payroll["child"])
    dm = strata.datamodule(payroll["child"])
    assert (dm.build_status, dm.access_lock) == ("ready", None)
    assert [m.subject for m in mail][-1] == "[STRATA] finance/summary: recovered"
    assert strata.client.get(f"/r/{payroll['report']}.csv", headers=VIEWER).status_code == 200


def test_gatekeeper_without_an_answer_locks_too(strata: Strata, payroll: dict[str, Any]) -> None:
    strata.app.config["GATEKEEPER_CLIENT"] = None
    _build(strata, payroll["child"])
    lock = _locked(strata, payroll["child"])
    assert lock is not None and lock.startswith("access could not be checked:")


def test_a_dependant_of_a_locked_datamodule_fails_but_is_not_locked(
    strata: Strata, payroll: dict[str, Any]
) -> None:
    grandchild = strata.new_datamodule(
        path="finance/board", script="CREATE TABLE b AS SELECT * FROM s.totals"
    )
    strata.set_access(grandchild, owners="", viewers=None)  # the admins
    strata.add_dependency(grandchild, payroll["child"], "s")
    _lose_access(payroll)
    _build(strata, payroll["child"])
    _build(strata, grandchild)
    dm = strata.datamodule(grandchild)
    assert dm.build_status == "failed" and dm.access_lock is None
    assert dm.last_build_error == (
        "dependency 's': 'finance/summary' is locked (its owners are fixing who may read it), "
        "so it cannot be read"
    )


def test_a_dependant_rebuilding_a_parent_that_locks_is_not_locked(
    strata: Strata, payroll: dict[str, Any]
) -> None:
    """With all dependencies: the parent's own check locks the parent only."""
    grandchild = strata.new_datamodule(
        path="finance/board", script="CREATE TABLE b AS SELECT * FROM s.totals"
    )
    strata.set_access(grandchild, owners="", viewers=None)
    strata.add_dependency(grandchild, payroll["child"], "s")
    _lose_access(payroll)
    strata.client.post(f"/datamodules/{grandchild}/build", data={"cascade": "1"})
    assert _locked(strata, payroll["child"]) == REASON
    dm = strata.datamodule(grandchild)
    assert dm.build_status == "failed" and dm.access_lock is None


def test_a_stale_read_that_locks_it_serves_nothing(strata: Strata, payroll: dict[str, Any]) -> None:
    child = payroll["child"]
    dm = strata.datamodule(child)
    strata.client.post(
        f"/datamodules/{child}/edit",
        data={"path": dm.path, "build_script": dm.build_script, "ttl_seconds": "0"},
    )
    _lose_access(payroll)
    resp = strata.client.get(f"/r/{payroll['report']}.csv", headers=VIEWER)  # rebuilds: locks
    assert resp.status_code == 403 and "350" not in resp.get_data(as_text=True)
    assert _locked(strata, child) == REASON


def test_a_scheduled_check_on_a_locked_datamodule_mails_no_rows(
    strata: Strata, payroll: dict[str, Any], mail: list[Any]
) -> None:
    rpt = strata.new_report(
        payroll["child"], body="SELECT * FROM totals", name="Big", macro="big", check="warning"
    )
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _lose_access(payroll)
    _build(strata, payroll["child"])
    mail.clear()
    _fire(strata)
    [run] = _runs(strata, rpt)
    assert run.status == "error" and run.error == f"the datamodule is locked: {REASON}"
    assert run.snapshot_path is None and run.version_id is None
    [sent] = mail
    assert sent.to == ["root@example.com"]  # the alert list (the admins), not jsmith
    assert "350" not in sent.html


def test_a_dependency_every_owner_cannot_read_is_not_added(
    strata: Strata, payroll: dict[str, Any]
) -> None:
    other = strata.new_datamodule(path="finance/other")
    strata.set_access(other, owners="alice\nbob", viewers=None)
    strata.add_dependency(other, payroll["parent"], "pay")
    assert (
        "error",
        "Not added: bob may not read 'finance/payroll' (dependency pay).",
    ) in strata.flashes()


def test_new_owners_must_be_able_to_read_every_parent(
    strata: Strata, payroll: dict[str, Any]
) -> None:
    resp = strata.set_access(payroll["child"], owners="alice\nbob", headers=ALICE)
    assert resp.status_code == 200
    assert "Access: bob may not read &#39;finance/payroll&#39; (dependency pay)." in (
        resp.get_data(as_text=True)
    )


def test_narrowing_a_parent_warns_about_its_dependants(
    strata: Strata, payroll: dict[str, Any]
) -> None:
    resp = strata.set_access(payroll["parent"], owners="pam", viewers="")
    assert resp.status_code == 302
    assert (
        "error",
        "These datamodules depend on this one and their next build fails and locks them: "
        "finance/summary: alice may not read 'finance/payroll' (dependency pay).",
    ) in strata.flashes()


def test_the_preview_follows_the_build_rule(strata: Strata, payroll: dict[str, Any]) -> None:
    script = "{% for r in rows('pay', 'SELECT amount FROM salaries') %}SELECT {{ r.amount }};{% endfor %}"
    _lose_access(payroll)
    resp = strata.client.post(
        f"/datamodules/{payroll['child']}/preview-script", data={"build_script": script}
    )
    text = resp.get_data(as_text=True).replace("&#39;", "'")
    assert f"Not previewed: {REASON}" in text and "SELECT 250" not in text


def test_checks_shows_a_locked_datamodule(strata: Strata, payroll: dict[str, Any]) -> None:
    strata.new_report(
        payroll["child"], body="SELECT * FROM totals", name="Big", macro="big", check="warning"
    )
    _lose_access(payroll)
    _build(strata, payroll["child"])
    page = strata.client.get("/checks", headers=VIEWER).get_data(as_text=True)
    assert 'class="health health-locked"' in page and "alice may not" not in page


def test_the_preview_refuses_a_locked_parent(strata: Strata, payroll: dict[str, Any]) -> None:
    grandchild = strata.new_datamodule(
        path="finance/board", script="CREATE TABLE b AS SELECT 1 AS x"
    )
    strata.set_access(grandchild, owners="", viewers=None)
    strata.add_dependency(grandchild, payroll["child"], "s")
    _lose_access(payroll)
    _build(strata, payroll["child"])
    script = "{% for r in rows('s', 'SELECT total FROM totals') %}SELECT {{ r.total }};{% endfor %}"
    resp = strata.client.post(
        f"/datamodules/{grandchild}/preview-script", data={"build_script": script}
    )
    text = resp.get_data(as_text=True).replace("&#39;", "'")
    assert "Not previewed: dependency 's' (finance/summary) is locked." in text
    assert "SELECT 350" not in text


def test_a_dependency_is_not_added_when_access_cannot_be_checked(
    strata: Strata, payroll: dict[str, Any]
) -> None:
    other = strata.new_datamodule(path="finance/other")
    strata.set_access(other, owners="alice", viewers=None)
    strata.app.config["GATEKEEPER_CLIENT"] = None
    strata.add_dependency(other, payroll["parent"], "pay")
    assert any(
        msg.startswith("Not added: access could not be checked: Gatekeeper is not configured")
        for _cat, msg in strata.flashes()
    )


def test_a_pinned_version_of_a_locked_datamodule_is_refused(
    strata: Strata, payroll: dict[str, Any]
) -> None:
    """?version= skips the freshness wait, so the lock is checked before it."""
    [version] = [v for v in strata.versions(payroll["child"]) if v.status == "ready"]
    _lose_access(payroll)
    _build(strata, payroll["child"])
    url = f"/r/{payroll['report']}.csv?version={version.uuid}"
    resp = strata.client.get(url)
    assert resp.status_code == 403 and "350" not in resp.get_data(as_text=True)
