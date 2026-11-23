"""Each datamodule's owners and viewers (AC1).

Owners (and the admins) edit a datamodule: every authoring route refuses anyone
else with 403. Writers create datamodules and must stay owners of what they make
and edit. A datamodule with no list of its own follows the access.default_*
settings as they change.
"""

from typing import Any

import pytest
from markupsafe import escape

from strata.services import access
from tests.conftest import FakeGatekeeper, Strata, as_user

WRITER = as_user("wendy", "strata-writers")
OUTSIDER = as_user("olly", "brs-finance")


def _save_access(
    strata: Strata,
    dm_uuid: str,
    owners: str | None = None,
    viewers: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Post the datamodule form as a browser would; a list of None ticks its
    "use the default" box."""
    dm = strata.datamodule(dm_uuid)
    data: dict[str, str] = {
        "path": dm.path,
        "build_script": dm.build_script,
        "ttl_seconds": "" if dm.ttl_seconds is None else str(dm.ttl_seconds),
        "access_present": "1",
        "owners": owners or "",
        "viewers": viewers or "",
    }
    if owners is None:
        data["owners_default"] = "1"
    if viewers is None:
        data["viewers_default"] = "1"
    return strata.client.post(f"/datamodules/{dm_uuid}/edit", data=data, headers=headers or {})


def _errors(strata: Strata) -> list[str]:
    return [msg for cat, msg in strata.flashes() if cat == "error"]


def _refused(resp: Any, message: str) -> None:
    """The form came back (200) showing ``message``."""
    assert resp.status_code == 200
    assert str(escape(message)) in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Every authoring route is for owners
# ---------------------------------------------------------------------------


def _authoring_urls(strata: Strata) -> list[tuple[str, str]]:
    """(method, url) for every route that names a datamodule, report, schedule or key."""
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    strata.new_api_key(dm_uuid)
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/schedules/new",
        data={
            "name": "s",
            "timezone": "Pacific/Auckland",
            "kind": "every",
            "every_n": "1",
            "every_unit": "hours",
            "enabled_present": "1",
            "enabled": "1",
        },
    )
    assert resp.status_code == 302
    with strata.app.app_context():
        from strata.models.api_key import ApiKey
        from strata.models.datamodule import Datamodule
        from strata.models.schedule import Schedule

        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        [schedule] = Schedule.for_datamodule(dm.id)
        [key] = ApiKey.get_for_datamodule(dm.id)
    [version] = strata.versions(dm_uuid)
    values = {
        "uuid": dm_uuid,
        "report_uuid": rpt,
        "schedule_uuid": schedule.uuid,
        "key_uuid": key.uuid,
        "version_uuid": version.uuid,
        "dep_id": "1",
        "link_id": "1",
        "source_id": "1",
    }
    urls: list[tuple[str, str]] = []
    for rule in strata.app.url_map.iter_rules():
        blueprint = rule.endpoint.partition(".")[0]
        if blueprint not in ("datamodules", "schedules") and rule.endpoint != "runs.run_now":
            continue
        if rule.endpoint in ("datamodules.index", "datamodules.new", "schedules.preview"):
            continue  # they name no datamodule
        url = rule.rule
        for arg in rule.arguments:
            url = url.replace(f"<{arg}>", values[arg]).replace(f"<int:{arg}>", values[arg])
        method = "GET" if "GET" in (rule.methods or ()) else "POST"
        urls.append((method, url))
    assert len(urls) >= 25, urls
    return urls


def test_every_authoring_route_is_for_owners(strata: Strata) -> None:
    for method, url in _authoring_urls(strata):
        resp = strata.client.open(url, method=method, headers=OUTSIDER)
        assert resp.status_code == 403, (method, url, resp.status_code)
        anon = strata.client.open(url, method=method, headers={"X-Anon": "1"})
        assert anon.status_code == 302 and "/auth/login" in anon.headers["Location"], url


def test_a_refusal_says_why_and_a_missing_datamodule_is_404(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=OUTSIDER)
    assert page.status_code == 403
    assert "Only this datamodule&#39;s owners and the Strata admins can do that." in page.get_data(
        as_text=True
    )
    missing = "00000000-0000-0000-0000-000000000000"
    assert strata.client.get(f"/datamodules/{missing}/edit", headers=OUTSIDER).status_code == 404
    assert strata.client.post(f"/reports/{missing}/run", headers=OUTSIDER).status_code == 404


def test_an_owner_who_is_not_an_admin_edits(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    assert _save_access(strata, dm_uuid, owners="Olly").status_code == 302
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=OUTSIDER)
    assert page.status_code == 200
    strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={"path": "test/renamed", "build_script": "", "ttl_seconds": ""},
        headers=OUTSIDER,
    )
    assert strata.datamodule(dm_uuid).path == "test/renamed"
    # ...but a writer who is not one of its owners may not
    assert strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=WRITER).status_code == 403


def test_the_list_shows_what_the_user_may_edit(strata: Strata) -> None:
    mine = strata.new_datamodule(path="test/mine")
    strata.new_datamodule(path="test/theirs")
    _save_access(strata, mine, owners="olly")
    page = strata.client.get("/datamodules/", headers=OUTSIDER).get_data(as_text=True)
    assert "test/mine" in page and "test/theirs" not in page
    admin_page = strata.client.get("/datamodules/").get_data(as_text=True)
    assert "test/mine" in admin_page and "test/theirs" in admin_page


# ---------------------------------------------------------------------------
# Writers create; owners stay owners
# ---------------------------------------------------------------------------


def _create(strata: Strata, headers: dict[str, str], **form: str) -> Any:
    data = {"path": "test/new", "build_script": "", "ttl_seconds": "", **form}
    return strata.client.post("/datamodules/new", data=data, headers=headers)


def test_writers_create_datamodules_and_others_may_not(strata: Strata) -> None:
    assert strata.client.get("/datamodules/new", headers=OUTSIDER).status_code == 403
    anon = strata.client.get("/datamodules/new", headers={"X-Anon": "1"})
    assert anon.status_code == 302 and "/auth/login" in anon.headers["Location"]
    assert _create(strata, OUTSIDER).status_code == 403
    resp = _create(strata, WRITER)
    assert resp.status_code == 302, _errors(strata)
    dm_uuid = resp.headers["Location"].split("/")[-2]
    dm = strata.datamodule(dm_uuid)
    assert (dm.owners, dm.viewers, dm.created_by) == (None, None, "wendy")
    assert strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=WRITER).status_code == 200


def test_a_writer_must_own_what_they_create(strata: Strata) -> None:
    strata.set_config({"access.default_owners": "@it"})
    _refused(
        _create(strata, WRITER),
        "Access: you would no longer be an owner. Keep yourself, or a group you are in, in Owners.",
    )
    form = {"access_present": "1", "owners": "wendy\n@it", "viewers": "@finance"}
    resp = _create(strata, WRITER, **form)
    assert resp.status_code == 302, _errors(strata)
    dm = strata.datamodule(resp.headers["Location"].split("/")[-2])
    assert (dm.owners, dm.viewers) == (["wendy", "@it"], ["@finance"])


def test_an_owner_may_not_remove_themselves_but_an_admin_may(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    _save_access(strata, dm_uuid, owners="olly")
    _refused(
        _save_access(strata, dm_uuid, owners="alice", headers=OUTSIDER),
        "you would no longer be an owner",
    )
    assert strata.datamodule(dm_uuid).owners == ["olly"]
    # an owner through a group may hand it on
    assert _save_access(strata, dm_uuid, owners="@brs-finance", headers=OUTSIDER).status_code == 302
    assert _save_access(strata, dm_uuid, owners="").status_code == 302  # admin: admins only
    assert strata.datamodule(dm_uuid).owners == []


# ---------------------------------------------------------------------------
# The lists
# ---------------------------------------------------------------------------


def test_the_lists_are_saved_and_a_ticked_box_follows_the_default(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    assert (
        _save_access(strata, dm_uuid, owners="alice\n@payroll", viewers="@finance").status_code
        == 302
    )
    dm = strata.datamodule(dm_uuid)
    assert (dm.owners, dm.viewers) == (["alice", "@payroll"], ["@finance"])
    strata.set_script(dm_uuid, "CREATE TABLE t AS SELECT 1 AS x")  # no Access fieldset
    dm = strata.datamodule(dm_uuid)
    assert (dm.owners, dm.viewers) == (["alice", "@payroll"], ["@finance"])
    _save_access(strata, dm_uuid, owners="alice", viewers=None)
    dm = strata.datamodule(dm_uuid)
    assert (dm.owners, dm.viewers) == (["alice"], None)


def test_the_defaults_are_live(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()  # on the defaults
    assert strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=WRITER).status_code == 200
    strata.set_config({"access.default_owners": "olly"})
    assert strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=WRITER).status_code == 403
    assert strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=OUTSIDER).status_code == 200


@pytest.mark.parametrize(
    ("owners", "viewers", "error"),
    [
        (
            "two words",
            "",
            "Access: 'two words' is not a username, an @group or * (everyone signed in).",
        ),
        ("*", "", "Access: owners are people or groups; * (everyone signed in) can only view."),
    ],
)
def test_a_bad_list_is_refused(strata: Strata, owners: str, viewers: str, error: str) -> None:
    dm_uuid = strata.new_datamodule()
    _refused(_save_access(strata, dm_uuid, owners=owners, viewers=viewers), error)
    assert strata.datamodule(dm_uuid).owners is None


def test_a_name_gatekeeper_does_not_know_is_refused(strata: Strata) -> None:
    strata.app.config["GATEKEEPER_CLIENT"] = FakeGatekeeper()
    dm_uuid = strata.new_datamodule()
    _refused(
        _save_access(strata, dm_uuid, owners="jsmith\nnobody", viewers="@nogroup"),
        "Access: Gatekeeper has no user or group nobody, @nogroup.",
    )
    assert (
        _save_access(strata, dm_uuid, owners="jsmith", viewers="@brs-finance\n*").status_code == 302
    )


def test_names_are_not_saved_when_gatekeeper_cannot_check_them(strata: Strata) -> None:
    class Broken(FakeGatekeeper):
        def get_user(self, username: str) -> Any:
            raise OSError("unable to open database file")

    strata.app.config["GATEKEEPER_CLIENT"] = Broken()
    dm_uuid = strata.new_datamodule()
    _refused(
        _save_access(strata, dm_uuid, owners="jsmith"),
        "Access: the names could not be checked: Gatekeeper could not look up jsmith: "
        "unable to open database file.",
    )
    assert strata.datamodule(dm_uuid).owners is None


def test_the_page_shows_the_lists(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    _save_access(strata, dm_uuid, owners="alice\n@payroll", viewers=None)
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert ">alice\n@payroll</textarea>" in page
    assert 'name="viewers_default" value="1" checked' in page
    assert "Use the default viewers (everyone signed in)" in page


def test_owners_and_viewers_in_force(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    with strata.app.app_context():
        dm = strata.datamodule(dm_uuid)
        assert access.owners_of(dm) == ["@strata-writers"] and access.viewers_of(dm) == ["*"]


def test_viewers_view_and_owners_view_too(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    _save_access(strata, dm_uuid, owners="alice", viewers="@brs-finance")
    cases = [
        (as_user("alice"), True, True),
        (OUTSIDER, False, True),  # in @brs-finance
        (WRITER, False, False),
        ({}, True, True),  # the admin
    ]
    for headers, owns, views in cases:
        with strata.app.test_request_context(headers=headers):
            strata.app.preprocess_request()
            dm = strata.datamodule(dm_uuid)
            assert (access.can_own(dm), access.can_view(dm)) == (owns, views), headers
