"""Who may do what: the instance roles, and each datamodule's owners and viewers.

Roles. **Admins** (``auth.admin_group``) may do everything everywhere. **Writers**
(``auth.writer_group``) may also create datamodules. Everyone else signed in is at
most a viewer.

Lists. A datamodule's owners and viewers are lists of **principals**: a username
(``jsmith``, or with its domain as Gatekeeper has it, ``asiap\\jsmith``), an
``@group``, or ``*`` for everyone signed in. Usernames compare ignoring case, as
Gatekeeper does. A datamodule with no list of its own follows the live settings
``access.default_owners`` (blank = ``@<auth.writer_group>``) and
``access.default_viewers`` (default ``*``).

Two kinds of question:

- **A request** asks about the signed-in user (``g.user``, whose groups Gatekeeper
  attached at sign-in): ``matches``, ``is_admin``, ``is_writer``.
- **A rule** asks whether everyone in one list is also in another (each owner of a
  dependant must be able to read its parent). That needs the members of groups,
  which only Gatekeeper knows: ``Directory``. It decides from the lists alone
  whenever it can (``*``, a group or user listed on both sides) and asks Gatekeeper
  only for the rest. If Gatekeeper cannot answer, it raises ``DirectoryUnavailable``
  and the caller refuses: no answer is never a yes.
"""

import re
from collections.abc import Iterable
from typing import Any

from flask import current_app, g

from strata.config import live_list, live_str
from strata.models.connection import Connection
from strata.models.datamodule import Datamodule

EVERYONE = "*"

PRINCIPAL_RE = re.compile(r"^(\*|@[\w.\- ]+|(?:[\w.\-]+\\)?[\w.\-]+)$")


class DirectoryUnavailable(RuntimeError):
    """Gatekeeper could not say who is in a group (or is not configured)."""


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def principal_lines(text: str) -> list[str]:
    """The non-blank lines of a list textarea, without repeats (ignoring case)."""
    seen: set[str] = set()
    entries: list[str] = []
    for line in text.splitlines():
        entry = line.strip()
        if entry and entry.lower() not in seen:
            seen.add(entry.lower())
            entries.append(entry)
    return entries


def check_principals(entries: Iterable[str]) -> str | None:
    """Why the first malformed entry is refused, or None."""
    for entry in entries:
        if not PRINCIPAL_RE.match(entry):
            return f"'{entry}' is not a username, an @group or * (everyone signed in)."
    return None


def describe(principal: str) -> str:
    return "everyone signed in" if principal == EVERYONE else principal


def writer_group() -> str:
    return live_str("auth.writer_group")


def admin_group() -> str:
    return str(current_app.config.get("AUTH_ADMIN_GROUP") or "strata-admins")


def default_owners() -> list[str]:
    """Owners of a datamodule with no list of its own: the setting, else the writers."""
    return live_list("access.default_owners") or [f"@{writer_group()}"]


def default_viewers() -> list[str]:
    return live_list("access.default_viewers")


# ---------------------------------------------------------------------------
# The signed-in user
# ---------------------------------------------------------------------------


def matches(user: Any, principals: Iterable[str]) -> bool:
    """Is ``user`` (a Gatekeeper user with its groups) named by ``principals``?"""
    if user is None:
        return False
    name = str(user.username).lower()
    for principal in principals:
        if principal == EVERYONE:
            return True
        if principal.startswith("@"):
            if user.in_group(principal[1:]):
                return True
        elif principal.lower() == name:
            return True
    return False


def is_admin() -> bool:
    return bool(g.get("strata_is_admin"))


def is_writer() -> bool:
    """Admins and members of ``auth.writer_group``: they may create datamodules."""
    user = g.get("user")
    return user is not None and (is_admin() or user.in_group(writer_group()))


# ---------------------------------------------------------------------------
# A datamodule's lists, and the signed-in user's place in them
# ---------------------------------------------------------------------------


def owners_of(dm: Datamodule) -> list[str]:
    """The owners in force: the datamodule's own list, else the default owners."""
    return dm.owners if dm.owners is not None else default_owners()


def viewers_of(dm: Datamodule) -> list[str]:
    """The viewers in force (owners view too, and are not repeated here)."""
    return dm.viewers if dm.viewers is not None else default_viewers()


def can_own(dm: Datamodule) -> bool:
    """May the signed-in user edit ``dm`` (script, reports, schedules, keys, access)?"""
    return is_admin() or matches(g.get("user"), owners_of(dm))


def can_view(dm: Datamodule) -> bool:
    """May the signed-in user open ``dm``'s reports and runs?"""
    return can_own(dm) or matches(g.get("user"), viewers_of(dm))


def may_use(connection: Connection) -> bool:
    """May the signed-in user bind ``connection`` to a datamodule they own?"""
    return is_admin() or matches(g.get("user"), connection.users or [])


def datamodule_for(view_args: dict[str, Any]) -> Datamodule | None:
    """The datamodule a URL is about: by its own uuid, or a report's, a schedule's or
    an API key's. None when the thing named does not exist."""
    from strata.models.api_key import ApiKey
    from strata.models.report import Report
    from strata.models.schedule import Schedule

    if "uuid" in view_args:
        return Datamodule.get_by_uuid(str(view_args["uuid"]))
    dm_id: int | None = None
    if "report_uuid" in view_args:
        report = Report.get_by_uuid(str(view_args["report_uuid"]))
        dm_id = report.datamodule_id if report else None
    elif "schedule_uuid" in view_args:
        schedule = Schedule.get_by_uuid(str(view_args["schedule_uuid"]))
        dm_id = schedule.datamodule_id if schedule else None
    elif "key_uuid" in view_args:
        key = ApiKey.get_by_uuid(str(view_args["key_uuid"]))
        if key is not None and key.datamodule_id is not None:
            dm_id = key.datamodule_id
        elif key is not None and key.report_id is not None:
            report = Report.get_by_id(key.report_id)
            dm_id = report.datamodule_id if report else None
    else:
        raise LookupError(f"no datamodule in the URL arguments {sorted(view_args)}")
    return Datamodule.get_by_id(dm_id) if dm_id is not None else None


# ---------------------------------------------------------------------------
# Rules over lists: Gatekeeper lists the groups
# ---------------------------------------------------------------------------


class Directory:
    """Group members from Gatekeeper, remembered for the life of one check (a build's
    check or a form save), so a group named twice is listed once."""

    def __init__(self) -> None:
        self.gk = current_app.config.get("GATEKEEPER_CLIENT")
        self.admins = admin_group()
        self._members: dict[str, frozenset[str]] = {}

    def members(self, group: str) -> frozenset[str]:
        """The usernames in ``group`` (lowercased). An unknown group has none."""
        if group in self._members:
            return self._members[group]
        if self.gk is None:
            raise DirectoryUnavailable(
                f"Gatekeeper is not configured, so Strata cannot tell who is in @{group}"
            )
        try:
            names = frozenset(str(u).lower() for u in self.gk.get_group_members(group))
        except Exception as exc:  # noqa: BLE001 -- any failure is "no answer"
            raise DirectoryUnavailable(f"Gatekeeper could not list @{group}: {exc}") from exc
        self._members[group] = names
        return names

    def missing(self, subjects: Iterable[str], audience: Iterable[str]) -> list[str]:
        """Who among ``subjects`` is not in ``audience``: usernames (lowercased), or
        ``*`` when everyone is a subject and not everyone is in the audience. Admins
        are in every audience. Raises DirectoryUnavailable when the answer needs
        Gatekeeper and it cannot give one."""
        audience = list(audience)
        if EVERYONE in audience:
            return []
        subjects = list(subjects)
        if EVERYONE in subjects:
            return [EVERYONE]
        groups = [p[1:] for p in audience if p.startswith("@")] + [self.admins]
        users = {p.lower() for p in audience if not p.startswith("@")}

        def covered(person: str) -> bool:
            return person in users or any(person in self.members(grp) for grp in groups)

        missing: list[str] = []
        for subject in subjects:
            if subject.startswith("@"):
                if subject[1:] in groups:
                    continue  # the same group is on both sides
                people = sorted(self.members(subject[1:]))
            else:
                people = [subject.lower()]
            missing.extend(p for p in people if p not in missing and not covered(p))
        return missing

    def unknown(self, principals: Iterable[str]) -> list[str]:
        """The users and groups Gatekeeper does not know (a typo in an access list is
        refused). Without Gatekeeper nothing can be checked, so nothing is unknown."""
        if self.gk is None:
            return []
        unknown: list[str] = []
        for principal in principals:
            if principal == EVERYONE:
                continue
            try:
                if principal.startswith("@"):
                    found = self.gk.get_group(principal[1:]) is not None
                else:
                    found = self.gk.get_user(principal) is not None
            except Exception as exc:  # noqa: BLE001
                raise DirectoryUnavailable(
                    f"Gatekeeper could not look up {principal}: {exc}"
                ) from exc
            if not found:
                unknown.append(principal)
        return unknown


# ---------------------------------------------------------------------------
# A datamodule's inputs: its owners must be allowed every one
# ---------------------------------------------------------------------------


class AccessError(RuntimeError):
    """A datamodule's owners are not allowed one of its inputs (or that could not be
    checked): it must not build, and it is locked. ``dm_id`` says which datamodule, so a
    dependant whose parent's build raised it fails without being locked itself."""

    def __init__(self, dm_id: int, message: str) -> None:
        super().__init__(message)
        self.dm_id = dm_id


def _names(people: list[str]) -> str:
    return ", ".join(describe(p) for p in people)


def parent_problem(
    owners: list[str], parent: Datamodule, local_name: str, directory: Directory
) -> str | None:
    """Why ``owners`` may not depend on ``parent``: one of them may not read it."""
    missing = directory.missing(owners, owners_of(parent) + viewers_of(parent))
    if not missing:
        return None
    return f"{_names(missing)} may not read '{parent.path}' (dependency {local_name})"


def input_problems(dm: Datamodule, owners: list[str], directory: Directory) -> list[str]:
    """Why ``owners`` may not own ``dm``'s inputs, one line each: an owner who may not
    read a parent (its owners or viewers) or use a bound connection. Raises
    DirectoryUnavailable when that cannot be told."""
    from strata.models.datamodule_connection import DatamoduleConnection
    from strata.models.dependency import Dependency

    problems: list[str] = []
    for dep in Dependency.get_for_datamodule(dm.id):
        parent = Datamodule.get_by_id(dep.depends_on_id)
        if parent is None:
            continue  # the build reports a deleted dependency itself
        problem = parent_problem(owners, parent, dep.local_name, directory)
        if problem:
            problems.append(problem)
    for binding in DatamoduleConnection.get_for_datamodule(dm.id):
        crow = Connection.get_by_id(binding.connection_id)
        if crow is None:
            continue  # the build reports a deleted connection itself
        missing = directory.missing(owners, crow.users or [])
        if missing:
            problems.append(
                f"{_names(missing)} may not use the connection '{crow.name}' "
                f"(bound as {binding.local_name})"
            )
    return problems


def check_build(dm: Datamodule) -> None:
    """Raise AccessError unless every owner of ``dm`` may read each parent and use each
    connection. Run at every build: group membership changes in Gatekeeper, out of
    Strata's sight."""
    try:
        problems = input_problems(dm, owners_of(dm), Directory())
    except DirectoryUnavailable as exc:
        raise AccessError(dm.id, f"access could not be checked: {exc}") from exc
    if problems:
        raise AccessError(dm.id, "; ".join(problems))


LOCKED = "This datamodule is locked: it serves nothing until its owners fix who may read it."
