"""The top nav follows the pattern shared with the sibling apps: a hamburger
button for narrow screens, the username linking to Gatekeeper's account page
(where people sign out), and no Logout link of its own."""

from tests.conftest import FakeGatekeeper, Strata


def _nav(page: str) -> str:
    return page[page.index("<nav>") : page.index("</nav>")]


def test_the_nav_has_a_menu_button_and_links_the_user_to_gatekeeper(strata: Strata) -> None:
    nav = _nav(strata.client.get("/checks").get_data(as_text=True))
    assert 'class="nav-toggle"' in nav and 'aria-controls="nav-menu"' in nav
    assert '<ul id="nav-menu" class="nav-menu">' in nav
    assert '<a href="/gatekeeper/auth/whoami" title="Your account">tester</a>' in nav
    assert "Logout" not in nav and "/auth/logout" not in nav
    assert '<a href="/admin/">Admin</a>' in nav


def test_the_account_link_sits_beside_gatekeepers_login(strata: Strata) -> None:
    strata.app.config["GATEKEEPER_CLIENT"] = FakeGatekeeper()
    nav = _nav(strata.client.get("/checks").get_data(as_text=True))
    assert 'href="https://reports.example.com/gatekeeper/auth/whoami"' in nav


def test_signed_out_the_nav_offers_login_only(strata: Strata) -> None:
    nav = _nav(strata.client.get("/auth/login", headers={"X-Anon": "1"}).get_data(as_text=True))
    assert 'href="/auth/login"' in nav and "Datamodules" not in nav and "whoami" not in nav
