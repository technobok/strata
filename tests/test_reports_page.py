"""The viewer's interface (AC5): the home page lists every report the user can
open, by datamodule; the nav offers each role only what it can use."""

import re

from tests.conftest import Strata, as_user

ALICE = as_user("alice")
VIEWER = as_user("vera", "brs-finance")
WRITER = as_user("wendy", "strata-writers")
OUTSIDER = as_user("olly")


def _nav(page: str) -> list[str]:
    menu = page.split('<ul id="nav-menu"', 1)[1].split("</ul>", 1)[0]
    return re.findall(r"<li><a [^>]*>([^<]+)</a></li>", menu)


def _home(strata: Strata, headers: dict[str, str]) -> str:
    resp = strata.client.get("/", headers=headers)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_home_lists_the_reports_the_user_can_open(strata: Strata) -> None:
    secret = strata.new_datamodule(path="finance/payroll")
    strata.new_report(secret, name="Salaries")
    strata.set_access(secret, owners="alice", viewers="@brs-finance")
    shared = strata.new_datamodule(path="sales/daily")
    strata.new_report(shared, name="Daily sales")
    strata.new_report(shared, name="Negative revenue", check="error")
    strata.new_datamodule(path="sales/empty")  # no reports: not listed

    page = _home(strata, OUTSIDER)
    assert "sales/daily" in page and "Daily sales" in page and "sales/empty" not in page
    assert "finance/payroll" not in page and "Salaries" not in page
    assert '<span class="severity severity-error">error</span>' in page
    assert "/datamodules/" not in page  # no datamodule links, no New Datamodule

    page = _home(strata, VIEWER)
    assert "finance/payroll" in page and "Salaries" in page
    assert f"/datamodules/{secret}/edit" not in page
    assert f"/datamodules/{secret}/edit" in _home(strata, ALICE)  # its owner


def test_home_marks_a_locked_datamodule(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.new_report(dm_uuid)
    with strata.app.app_context():
        from strata.db import get_db

        get_db().execute("UPDATE datamodule SET access_lock = 'x' WHERE uuid = ?", (dm_uuid,))
    assert 'severity-error" title="It serves nothing' in _home(strata, OUTSIDER)


def test_writers_get_new_datamodule_on_home(strata: Strata) -> None:
    assert 'href="/datamodules/new"' in _home(strata, WRITER)
    assert 'href="/datamodules/new"' not in _home(strata, OUTSIDER)


def test_anonymous_home_asks_to_log_in(strata: Strata) -> None:
    page = _home(strata, {"X-Anon": "1"})
    assert "to see your reports" in page and _nav(page) == ["Login"]


def test_the_nav_offers_each_role_what_it_can_use(strata: Strata) -> None:
    owned = strata.new_datamodule()
    strata.set_access(owned, owners="alice")
    assert _nav(_home(strata, OUTSIDER)) == ["Reports", "Checks", "Help", "olly"]
    assert _nav(_home(strata, WRITER)) == ["Reports", "Datamodules", "Checks", "Help", "wendy"]
    # an owner who is not a writer still reaches the datamodules they own
    assert _nav(_home(strata, ALICE)) == ["Reports", "Datamodules", "Checks", "Help", "alice"]
    # Tags (admin-only, attached to nothing) live on the Admin page: the admin's nav
    # keeps the item count the hamburger breakpoint was measured for.
    assert _nav(_home(strata, {})) == [
        "Reports",
        "Datamodules",
        "Checks",
        "Admin",
        "Help",
        "tester",
    ]
    assert 'href="/tags/" role="button"' in strata.client.get("/admin/").get_data(as_text=True)
