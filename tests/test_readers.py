"""Who may read a datamodule's reports, runs and checks (AC2).

Its viewers and owners (and the admins) open its reports in every format, its
runs, its checks on /checks and its Excel box, and may rebuild it. Anyone else
signed in gets 403, and never starts a build. Keys (Excel links) keep working
for whoever holds them: that is what they are for.
"""

from typing import Any

from strata.models.build_job import BuildJob
from tests.conftest import Strata, as_user
from tests.test_excel_links import _box, _links, _local

OWNER = as_user("alice")
VIEWER = as_user("vera", "brs-finance")
OUTSIDER = as_user("olly")
ANON = {"X-Anon": "1"}


def _restricted(strata: Strata, ttl: str = "") -> tuple[str, str]:
    """A built datamodule owned by alice, viewed by @brs-finance, with one report."""
    dm_uuid = strata.new_datamodule(ttl=ttl)
    rpt = strata.new_report(dm_uuid)
    strata.build(dm_uuid)
    assert strata.set_access(dm_uuid, owners="alice", viewers="@brs-finance").status_code == 302
    return dm_uuid, rpt


def _jobs(strata: Strata, dm_uuid: str) -> list[BuildJob]:
    with strata.app.app_context():
        return BuildJob.recent_for_datamodule(strata.datamodule(dm_uuid).id)


def test_viewers_read_the_report_and_others_get_403(strata: Strata) -> None:
    _dm, rpt = _restricted(strata)
    for headers in (OWNER, VIEWER, {}):
        for ext in ("html", "csv", "parquet"):
            assert strata.client.get(f"/r/{rpt}.{ext}", headers=headers).status_code == 200
    page = strata.client.get(f"/r/{rpt}.html", headers=OUTSIDER)
    assert page.status_code == 403
    assert "You are not one of this report&#39;s viewers" in page.get_data(as_text=True)
    data = strata.client.get(f"/r/{rpt}.csv", headers=OUTSIDER)
    assert data.status_code == 403 and "North" not in data.get_data(as_text=True)


def test_a_non_viewer_never_starts_a_build(strata: Strata) -> None:
    dm_uuid, rpt = _restricted(strata, ttl="0")  # always stale: a read would rebuild
    before = len(_jobs(strata, dm_uuid))
    assert strata.client.get(f"/r/{rpt}.csv", headers=OUTSIDER).status_code == 403
    assert strata.client.get(f"/r/{rpt}.html", headers=OUTSIDER).status_code == 403
    assert len(_jobs(strata, dm_uuid)) == before
    assert strata.client.get(f"/r/{rpt}.csv", headers=VIEWER).status_code == 200
    assert len(_jobs(strata, dm_uuid)) == before + 1


def test_an_excel_link_works_for_whoever_holds_it(strata: Strata) -> None:
    _dm, rpt = _restricted(strata)
    csv = _local(_links(_box(strata, rpt, headers=VIEWER))["CSV"])
    assert strata.client.get(csv, headers=ANON).status_code == 200


def test_the_excel_box_is_for_viewers_and_replace_for_owners(strata: Strata) -> None:
    _dm, rpt = _restricted(strata)
    assert strata.client.get(f"/r/{rpt}/excel", headers=OUTSIDER).status_code == 403
    assert "Replace the datamodule's Excel key" not in _box(strata, rpt, headers=VIEWER)
    assert strata.client.post(f"/r/{rpt}/excel/replace", headers=VIEWER).status_code == 403
    assert "Replace the datamodule's Excel key" in _box(strata, rpt, headers=OWNER)
    assert strata.client.post(f"/r/{rpt}/excel/replace", headers=OWNER).status_code == 200


def _run(strata: Strata, rpt: str) -> Any:
    resp = strata.client.post(f"/reports/{rpt}/run")
    assert resp.status_code == 302
    return resp.headers["Location"].rsplit("/", 1)[-1]


def test_runs_are_for_viewers(strata: Strata) -> None:
    _dm, rpt = _restricted(strata)
    run_uuid = _run(strata, rpt)
    for url in (f"/runs/{run_uuid}", f"/runs/{run_uuid}.csv"):
        assert strata.client.get(url, headers=VIEWER).status_code == 200
        assert strata.client.get(url, headers=OUTSIDER).status_code == 403


def test_checks_shows_only_what_the_user_may_view(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.new_report(dm_uuid, name="Secret check", macro="secret", check="warning")
    strata.set_access(dm_uuid, owners="alice", viewers="@brs-finance")
    assert "Secret check" in strata.client.get("/checks", headers=VIEWER).get_data(as_text=True)
    assert "Secret check" not in strata.client.get("/checks", headers=OUTSIDER).get_data(
        as_text=True
    )


def test_viewers_build_now_from_the_report_page(strata: Strata) -> None:
    dm_uuid, rpt = _restricted(strata)
    page = strata.client.get(f"/r/{rpt}.html", headers=VIEWER).get_data(as_text=True)
    assert f'action="/r/{rpt}/build"' in page
    assert "/edit" not in page  # no Edit, no link into the datamodule's page
    assert f'action="/reports/{rpt}/run"' not in page  # "Save a run" is for owners
    resp = strata.client.post(f"/r/{rpt}/build", headers=VIEWER)
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/r/{rpt}.html")
    assert len(strata.versions(dm_uuid)) == 2
    [job, *_] = _jobs(strata, dm_uuid)
    assert (job.reason, job.cascade) == ("manual:vera", False)
    assert strata.client.post(f"/r/{rpt}/build", headers=OUTSIDER).status_code == 403
    anon = strata.client.post(f"/r/{rpt}/build", headers=ANON)
    assert anon.status_code == 302 and "/auth/login" in anon.headers["Location"]
    assert len(strata.versions(dm_uuid)) == 2


def test_owners_see_edit_and_the_datamodule_link(strata: Strata) -> None:
    dm_uuid, rpt = _restricted(strata)
    page = strata.client.get(f"/r/{rpt}.html", headers=OWNER).get_data(as_text=True)
    assert f"/datamodules/reports/{rpt}/edit" in page and f"/datamodules/{dm_uuid}/edit" in page
    assert f'action="/reports/{rpt}/run"' in page


def test_the_dependency_picker_offers_only_what_the_user_may_read(strata: Strata) -> None:
    secret, _ = _restricted(strata)
    strata.new_datamodule(path="test/open")
    mine = strata.new_datamodule(path="test/mine")
    strata.set_access(mine, owners="olly", viewers=None)
    page = strata.client.get(f"/datamodules/{mine}/edit", headers=OUTSIDER).get_data(as_text=True)
    assert "test/open" in page and "test/sales" not in page
    resp = strata.client.post(
        f"/datamodules/{mine}/dependencies",
        data={"local_name": "s", "depends_on_uuid": secret},
        headers=OUTSIDER,
    )
    assert resp.status_code == 302
    assert ("error", "Choose a datamodule to depend on.") in strata.flashes()
    with strata.app.app_context():
        from strata.models.dependency import Dependency

        assert Dependency.get_for_datamodule(strata.datamodule(mine).id) == []
