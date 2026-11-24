"""The people fields: Gatekeeper's users and @groups suggested as you type
(``/people/suggest``, for those who edit such a field), and every list checked
against Gatekeeper when it is saved (recipients' email addresses are not looked up).
"""

import html
from typing import Any

from tests.conftest import FakeGatekeeper, GkUser, Strata, as_user
from tests.test_notify import _schedule

VIEWER = as_user("vic")  # signed in, not a writer, owns nothing


def _suggest(strata: Strata, q: str, headers: dict[str, str] | None = None) -> Any:
    return strata.client.get(f"/people/suggest?q={q}", headers=headers or {})


def test_users_are_found_by_name_username_or_email(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    gatekeeper.users["asiap\\pawe"] = GkUser("asiap\\pawe", "paul@example.com", fullname="Paul W")
    by_name = _suggest(strata, "paul w").get_json()
    assert by_name == [
        {"value": "asiap\\pawe", "text": "Paul W", "detail": "paul@example.com", "kind": "user"}
    ]
    assert [s["value"] for s in _suggest(strata, "pawe").get_json()] == ["asiap\\pawe"]
    assert [s["value"] for s in _suggest(strata, "example.com").get_json()] == [
        "alice",
        "asiap\\jdoe",
        "asiap\\pawe",
        "jsmith",
        "root",
    ]  # bob is disabled: never suggested


def test_groups_are_found_and_an_at_sign_asks_only_for_groups(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    gatekeeper.users["financebot"] = GkUser("financebot", "fb@example.com")
    found = _suggest(strata, "finance").get_json()
    assert [(s["value"], s["kind"]) for s in found] == [
        ("financebot", "user"),
        ("@brs-finance", "group"),
    ]
    only_groups = _suggest(strata, "%40finance").get_json()  # "@finance"
    assert [s["value"] for s in only_groups] == ["@brs-finance"]


def test_suggestions_are_for_those_who_edit_people_fields(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    gatekeeper.users["vic"] = GkUser("vic", "vic@example.com")
    assert _suggest(strata, "alice", VIEWER).status_code == 403
    dm_uuid = strata.new_datamodule()
    assert strata.set_access(dm_uuid, owners="vic").status_code == 302
    assert _suggest(strata, "alice", VIEWER).status_code == 200  # an owner now
    assert _suggest(strata, "alice", as_user("wendy", "strata-writers")).status_code == 200
    assert strata.client.get("/people/suggest?q=a", headers={"X-Anon": "1"}).status_code == 302


def test_without_gatekeeper_there_are_no_suggestions(strata: Strata) -> None:
    resp = _suggest(strata, "alice")
    assert resp.status_code == 200 and resp.get_json() == []


def test_a_schedule_refuses_an_unknown_user_or_group(
    strata: Strata, gatekeeper: FakeGatekeeper
) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    for wrong in ("jsmiht", "@brs-fiance"):
        resp = _schedule(strata, rpt, f"alice\n{wrong}")
        assert resp.status_code == 200
        page = html.unescape(resp.get_data(as_text=True))
        assert f"Gatekeeper has no user or group {wrong}." in page
    ok = _schedule(strata, rpt, "alice\n@brs-finance\nops@example.com")
    assert ok.status_code == 302  # an email address is not looked up


def test_an_alert_list_refuses_an_unknown_user(strata: Strata, gatekeeper: FakeGatekeeper) -> None:
    dm_uuid = strata.new_datamodule()
    dm = strata.datamodule(dm_uuid)
    form = {
        "path": dm.path,
        "build_script": dm.build_script,
        "ttl_seconds": "",
        "alerts_present": "1",
        "stale_alert_hours": "",
    }
    bad = strata.client.post(
        f"/datamodules/{dm_uuid}/edit", data={**form, "alert_recipients": "nobody-here"}
    )
    assert (
        "Alert list: Gatekeeper has no user or group nobody-here."
        in html.unescape(bad.get_data(as_text=True))
        or ("error", "Alert list: Gatekeeper has no user or group nobody-here.") in strata.flashes()
    )
    good = strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={**form, "alert_recipients": "jsmith\n@brs-finance\nops@example.com"},
    )
    assert good.status_code == 302
    assert strata.datamodule(dm_uuid).alert_recipients == [
        "jsmith",
        "@brs-finance",
        "ops@example.com",
    ]


def test_every_people_field_offers_suggestions(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    marker = 'data-people="/people/suggest"'
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    for field in ("alert_recipients", "owners", "viewers"):
        tag = page.split(f'<textarea name="{field}"', 1)[1].split(">", 1)[0]
        assert marker in tag, field
    schedule = strata.client.get(f"/datamodules/reports/{rpt}/schedules/new").get_data(as_text=True)
    assert marker in schedule.split('<textarea name="recipients"', 1)[1].split(">", 1)[0]
    connection = strata.client.get("/admin/connections/new").get_data(as_text=True)
    assert marker in connection.split('<textarea id="may_use"', 1)[1].split(">", 1)[0]
    assert "vendor/tom-select.min.js" in page and "js/people.js" in page
    assert "vendor/tom-select.min.css" in page
