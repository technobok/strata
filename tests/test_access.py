"""Who may do what (services/access): roles, principal lists, and the rules over
lists that need Gatekeeper to know who is in a group.

A list holds usernames (ignoring case; Gatekeeper's carry a domain), @groups and
* (everyone signed in). Admins are in every audience. A rule decides from the
lists alone when it can; when it must ask Gatekeeper and gets no answer, it
raises rather than guess.
"""

from typing import Any

import pytest
from flask import Flask

from strata.services import access
from tests.conftest import ADMIN_GROUP, FakeGatekeeper, FakeUser, Strata, as_user


def _gatekeeper(app: Flask, **groups: list[str]) -> FakeGatekeeper:
    gk = FakeGatekeeper()
    gk.groups.update(groups)
    app.config["GATEKEEPER_CLIENT"] = gk
    return gk


def test_a_list_holds_users_groups_and_everyone() -> None:
    good = ["jsmith", "asiap\\jsmith", "@brs-finance", "@Payroll Team", "*"]
    assert access.check_principals(good) is None
    for bad in ("two words", "@", "jsmith@example.com", "a\\b\\c", "**"):
        assert access.check_principals(["jsmith", bad]) == (
            f"'{bad}' is not a username, an @group or * (everyone signed in)."
        )


def test_list_lines_drop_blanks_and_repeats() -> None:
    assert access.principal_lines(" alice \n\nALICE\n@x\n  \n*\n") == ["alice", "@x", "*"]


def test_default_owners_are_the_writers_unless_set(strata: Strata) -> None:
    with strata.app.app_context():
        assert access.default_owners() == ["@strata-writers"]
        assert access.default_viewers() == ["*"]
    strata.set_config({"auth.writer_group": "brs-writers"})
    with strata.app.app_context():
        assert access.default_owners() == ["@brs-writers"]
    strata.set_config({"access.default_owners": "alice, @it", "access.default_viewers": "@staff"})
    with strata.app.app_context():
        assert access.default_owners() == ["alice", "@it"]
        assert access.default_viewers() == ["@staff"]


def test_a_user_matches_by_name_group_or_everyone() -> None:
    user = FakeUser(username="ASIAP\\JSmith", groups=frozenset({"brs-finance"}))
    assert access.matches(user, ["asiap\\jsmith"])
    assert access.matches(user, ["@brs-finance"])
    assert access.matches(user, ["*"])
    assert not access.matches(user, ["jsmith", "@payroll", "@brs"])
    assert not access.matches(None, ["*"])


@pytest.mark.parametrize(
    ("headers", "writer"),
    [
        ({}, True),  # the default test user is an admin
        (as_user("w", "strata-writers"), True),
        (as_user("v", "brs-finance"), False),
    ],
)
def test_writers_are_the_writer_group_and_admins(
    app: Flask, headers: dict[str, str], writer: bool
) -> None:
    with app.test_request_context(headers=headers):
        app.preprocess_request()
        assert access.is_writer() is writer


def test_a_writer_group_change_applies_at_once(strata: Strata) -> None:
    strata.set_config({"auth.writer_group": "brs-writers"})
    with strata.app.test_request_context(headers=as_user("w", "strata-writers")):
        strata.app.preprocess_request()
        assert not access.is_writer()


def test_a_rule_decided_by_the_lists_needs_no_gatekeeper(app: Flask) -> None:
    with app.app_context():
        directory = access.Directory()
        assert directory.gk is None
        assert directory.missing(["@anyone", "bob"], ["*"]) == []
        assert directory.missing(["@brs-finance", "Alice"], ["@brs-finance", "alice"]) == []
        assert directory.missing(["*"], ["@brs-finance"]) == ["*"]
        assert directory.missing([], ["@payroll"]) == []  # owners = admins only


def test_a_rule_lists_who_is_not_in_the_audience(app: Flask) -> None:
    _gatekeeper(app, payroll=["bob"])
    with app.app_context():
        directory = access.Directory()
        # alice is listed, bob is in @payroll, root is an admin
        assert directory.missing(["@brs-finance", "root"], ["alice", "@payroll"]) == [
            "carol",
            "jsmith",
        ]
        assert directory.missing([f"@{ADMIN_GROUP}"], ["@payroll"]) == []


def test_no_answer_from_gatekeeper_is_never_a_yes(app: Flask) -> None:
    with app.app_context():
        with pytest.raises(access.DirectoryUnavailable, match="not configured.*@payroll"):
            access.Directory().missing(["bob"], ["@payroll"])

    class Broken(FakeGatekeeper):
        def get_group_members(self, name: str) -> list[str]:
            raise OSError("unable to open database file")

    app.config["GATEKEEPER_CLIENT"] = Broken()
    with app.app_context():
        with pytest.raises(access.DirectoryUnavailable, match="could not list @payroll"):
            access.Directory().missing(["bob"], ["@payroll"])


def test_a_group_is_listed_once_per_check(app: Flask) -> None:
    calls: list[str] = []
    gk = _gatekeeper(app, payroll=["bob"])
    original = gk.get_group_members

    def counted(name: str) -> list[str]:
        calls.append(name)
        return original(name)

    gk.get_group_members = counted  # type: ignore[method-assign]
    with app.app_context():
        directory = access.Directory()
        directory.missing(["alice", "carol"], ["@payroll"])
    assert calls.count("payroll") == 1


def test_unknown_names_are_found(app: Flask) -> None:
    with app.app_context():
        assert access.Directory().unknown(["nobody", "@nogroup"]) == []  # cannot tell
    _gatekeeper(app)
    names: list[Any] = ["jsmith", "@brs-finance", "nobody", "@nogroup", "*", "asiap\\jdoe"]
    with app.app_context():
        assert access.Directory().unknown(names) == ["nobody", "@nogroup"]
