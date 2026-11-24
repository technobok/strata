"""Shared fixtures: a fresh Strata app per test, driven through its real routes.

Each test gets its own SQLite metadata DB and DATA_ROOT under pytest's tmp_path
(STRATA_DB / DATA_ROOT env vars are the app's test hook; DATA_ROOT outranks
test_config). Gatekeeper is not configured, so a before_request hook injects the
user instead:

- default: an admin (member of ``strata-admins``)
- ``X-User: <name>`` header: a signed-in user who is not an admin, in the groups
  listed in ``X-Groups: a,b`` (none if absent); ``as_user(name, *groups)`` builds
  both. ``X-User: viewer`` is the plain non-admin most tests use.
- ``X-Anon: 1`` header: no user at all (the API-key / anonymous path)
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask, g, request
from flask.testing import FlaskClient

ADMIN_GROUP = "strata-admins"

# A small build script used by most tests: one table, three rows.
SALES_SCRIPT = """
CREATE TABLE sales AS
SELECT * FROM (VALUES
    ('North', 'A', 100.0, DATE '2026-01-05'),
    ('South', 'B', 250.5, DATE '2026-02-10'),
    ('North', 'C', 75.25, DATE '2026-03-15')
) AS t(region, product, revenue, sold_on);
"""


@dataclass
class FakeUser:
    username: str = "tester"
    fullname: str = "Test User"
    groups: frozenset[str] = field(default_factory=lambda: frozenset({ADMIN_GROUP}))

    def in_group(self, name: str) -> bool:
        return name in self.groups


def as_user(name: str, *groups: str) -> dict[str, str]:
    """Request headers that sign in as ``name``, a non-admin in ``groups``."""
    return {"X-User": name, "X-Groups": ",".join(groups)}


@pytest.fixture
def app(tmp_path, monkeypatch) -> Iterator[Flask]:
    db_path = tmp_path / "strata.sqlite3"
    monkeypatch.setenv("STRATA_DB", str(db_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    for var in ("GATEKEEPER_DB", "STRATA_ADMIN_GROUP", "STRATA_ROOT"):
        monkeypatch.delenv(var, raising=False)

    from strata import create_app
    from strata.db import close_standalone_db, init_db_at

    init_db_at(str(db_path))
    # WORKER_INLINE: requested builds run synchronously in the caller (no worker).
    application = create_app({"TESTING": True, "SECRET_KEY": "test-secret", "WORKER_INLINE": True})

    def _inject_user() -> None:
        if request.headers.get("X-Anon"):
            return
        name = request.headers.get("X-User")
        if name:
            groups = frozenset(x for x in request.headers.get("X-Groups", "").split(",") if x)
            g.user = FakeUser(username=name, fullname=name.title(), groups=groups)
        else:
            g.user = FakeUser()

    # Must run before create_app's own _populate_is_admin hook.
    application.before_request_funcs.setdefault(None, []).insert(0, _inject_user)
    yield application
    close_standalone_db()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


class Strata:
    """Authoring helpers that go through the real admin routes."""

    def __init__(self, app: Flask, client: FlaskClient) -> None:
        self.app = app
        self.client = client

    def new_datamodule(
        self, path: str = "test/sales", script: str = SALES_SCRIPT, ttl: str = ""
    ) -> str:
        resp = self.client.post(
            "/datamodules/new", data={"path": path, "build_script": script, "ttl_seconds": ttl}
        )
        assert resp.status_code == 302, resp.get_data(as_text=True)
        match = re.search(r"/datamodules/([0-9a-f-]{36})/edit", resp.headers["Location"])
        assert match, resp.headers["Location"]
        return match.group(1)

    def build(self, dm_uuid: str) -> None:
        resp = self.client.post(f"/datamodules/{dm_uuid}/build")
        assert resp.status_code == 302

    def set_script(self, dm_uuid: str, script: str) -> None:
        dm = self.datamodule(dm_uuid)
        # A browser posts every field; keep the TTL (blank would clear it).
        ttl = "" if dm.ttl_seconds is None else str(dm.ttl_seconds)
        resp = self.client.post(
            f"/datamodules/{dm_uuid}/edit",
            data={"path": dm.path, "build_script": script, "ttl_seconds": ttl},
        )
        assert resp.status_code == 302

    def set_access(
        self,
        dm_uuid: str,
        owners: str | None = None,
        viewers: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Post the datamodule form with its Access fieldset, as a browser would. A list
        given as None is posted as the form shows it (unchanged); lines are
        newline-separated."""
        from strata.services import access

        dm = self.datamodule(dm_uuid)
        with self.app.app_context():
            shown_owners = "\n".join(access.form_owners(dm))
            shown_viewers = "\n".join(access.form_viewers(dm))
        data: dict[str, str] = {
            "path": dm.path,
            "build_script": dm.build_script,
            "ttl_seconds": "" if dm.ttl_seconds is None else str(dm.ttl_seconds),
            "access_present": "1",
            "owners": shown_owners if owners is None else owners,
            "viewers": shown_viewers if viewers is None else viewers,
        }
        return self.client.post(f"/datamodules/{dm_uuid}/edit", data=data, headers=headers or {})

    def add_dependency(self, dm_uuid: str, depends_on_uuid: str, local_name: str) -> None:
        resp = self.client.post(
            f"/datamodules/{dm_uuid}/dependencies",
            data={"local_name": local_name, "depends_on_uuid": depends_on_uuid},
        )
        assert resp.status_code == 302

    def upload_source(self, dm_uuid: str, filename: str, content: bytes) -> None:
        import io

        resp = self.client.post(
            f"/datamodules/{dm_uuid}/sources",
            data={"file": (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 302

    def new_report(
        self,
        dm_uuid: str,
        body: str = "SELECT * FROM sales",
        name: str = "Sales",
        args: list[dict[str, str]] | None = None,
        check: str | None = None,
        gate: bool = False,
    ) -> str:
        """Create a report; ``check`` = a severity makes it a check (``gate`` too)."""
        data: dict[str, Any] = {"name": name, "body_sql": body}
        if check is not None:
            data.update(is_check="on", severity=check)
            if gate:
                data["gate"] = "on"
        for key in ("name", "type", "widget", "default", "required", "constraint", "description"):
            data[f"arg_{key}"] = [a.get(key, "") for a in args or []]
        resp = self.client.post(f"/datamodules/{dm_uuid}/reports/new", data=data)
        assert resp.status_code == 302, resp.get_data(as_text=True)
        with self.app.app_context():
            from strata.models.datamodule import Datamodule
            from strata.models.report import Report

            dm = Datamodule.get_by_uuid(dm_uuid)
            assert dm is not None
            reports = [r for r in Report.get_for_datamodule(dm.id) if r.name == name]
            assert reports, self.flashes()
            return reports[0].uuid

    def new_api_key(self, dm_uuid: str, **form: str) -> str:
        """Make an admin key on the datamodule page; returns its token."""
        data = {"name": "excel", **form}
        resp = self.client.post(f"/datamodules/{dm_uuid}/apikeys", data=data)
        assert resp.status_code == 302
        with self.app.app_context():
            from strata.models.api_key import ApiKey
            from strata.models.datamodule import Datamodule

            dm = Datamodule.get_by_uuid(dm_uuid)
            assert dm is not None
            keys = [k for k in ApiKey.get_for_datamodule(dm.id) if k.name == data["name"]]
            assert keys, self.flashes()
            return keys[0].link_token()

    def set_config(self, settings: dict[str, str | int | bool]) -> None:
        """Save settings through /admin/config, as an admin would (no restart)."""
        from strata.config import ConfigType, resolve_entry

        data: dict[str, str] = {"action": "save"}
        for key, value in settings.items():
            entry = resolve_entry(key)
            assert entry is not None, key
            data[f"config_{key}_present"] = "1"
            if entry.type is ConfigType.BOOL:
                if value:
                    data[f"config_{key}"] = "on"
            else:
                data[f"config_{key}"] = str(value)
        resp = self.client.post("/admin/config", data=data)
        assert resp.status_code == 302
        errors = [msg for cat, msg in self.flashes() if cat == "error"]
        assert not errors, errors

    def flashes(self) -> list[tuple[str, str]]:
        with self.client.session_transaction() as sess:
            return list(sess.get("_flashes", []))

    def datamodule(self, dm_uuid: str) -> Any:
        with self.app.app_context():
            from strata.models.datamodule import Datamodule

            return Datamodule.get_by_uuid(dm_uuid)

    def versions(self, dm_uuid: str) -> list[Any]:
        with self.app.app_context():
            from strata.models.datamodule import Datamodule
            from strata.models.datamodule_version import DatamoduleVersion

            dm = Datamodule.get_by_uuid(dm_uuid)
            assert dm is not None
            return DatamoduleVersion.list_for(dm.id)


@pytest.fixture
def strata(app: Flask, client: FlaskClient) -> Strata:
    return Strata(app, client)


# ---------------------------------------------------------------------------
# Mail: a sink instead of Outbox, and a Gatekeeper to resolve names
# ---------------------------------------------------------------------------

PUBLIC_URL = "https://reports.example.com/strata"


@dataclass
class GkUser:
    username: str
    email: str
    enabled: bool = True
    fullname: str = ""


class FakeGatekeeper:
    def __init__(self) -> None:
        self.users = {
            "jsmith": GkUser("jsmith", "jsmith@example.com"),
            "alice": GkUser("alice", "alice@example.com"),
            "bob": GkUser("bob", "bob@example.com", enabled=False),
            "carol": GkUser("carol", ""),
            "root": GkUser("root", "root@example.com"),
            "asiap\\jdoe": GkUser("asiap\\jdoe", "jdoe@example.com"),
        }
        self.groups = {
            "brs-finance": ["alice", "bob", "carol", "jsmith"],
            ADMIN_GROUP: ["root"],
            "strata-writers": [],
        }

    def get_user(self, username: str) -> GkUser | None:
        return self.users.get(username)

    def list_users(
        self,
        search: str | None = None,
        enabled_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[GkUser]:
        """Like Gatekeeper's: ``search`` matches the username, email or full name."""
        needle = (search or "").lower()
        found = [
            u
            for u in self.users.values()
            if (not enabled_only or u.enabled)
            and any(needle in (v or "").lower() for v in (u.username, u.email, u.fullname))
        ]
        return sorted(found, key=lambda u: u.username)[offset : offset + limit]

    def list_groups(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=n, description=f"{n} group") for n in sorted(self.groups)]

    def get_group_members(self, name: str) -> list[str]:
        return self.groups.get(name, [])

    def get_group(self, name: str) -> SimpleNamespace | None:
        return SimpleNamespace(name=name) if name in self.groups else None

    def get_user_groups(self, username: str) -> list[str]:
        return sorted(
            group
            for group, members in self.groups.items()
            if username.lower() in (m.lower() for m in members)
        )

    def get_login_url(self) -> str | None:
        return "https://reports.example.com/gatekeeper/auth/login"


@pytest.fixture
def gatekeeper(strata: Strata) -> FakeGatekeeper:
    """A Gatekeeper that knows FakeGatekeeper's users and groups (add more to them)."""
    gk = FakeGatekeeper()
    strata.app.config["GATEKEEPER_CLIENT"] = gk
    return gk


@pytest.fixture
def mail(strata: Strata, gatekeeper: FakeGatekeeper) -> list[Any]:
    """Mail the app sends, instead of Outbox; and a Gatekeeper to resolve names."""
    sent: list[Any] = []
    strata.app.config["NOTIFY_SINK"] = sent.append
    strata.set_config({"server.public_url": PUBLIC_URL})
    return sent
