"""Shared fixtures: a fresh Strata app per test, driven through its real routes.

Each test gets its own SQLite metadata DB and DATA_ROOT under pytest's tmp_path
(STRATA_DB / DATA_ROOT env vars are the app's test hook; DATA_ROOT outranks
test_config). Gatekeeper is not configured, so a before_request hook injects the
user instead:

- default: an admin (member of ``strata-admins``)
- ``X-User: viewer`` header: a logged-in user who is not an admin
- ``X-Anon: 1`` header: no user at all (the API-key / anonymous path)
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
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
        if request.headers.get("X-User") == "viewer":
            g.user = FakeUser(username="viewer", fullname="Viewer", groups=frozenset())
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
        macro: str = "sales_report",
        args: list[dict[str, str]] | None = None,
    ) -> str:
        data: dict[str, Any] = {"name": name, "macro_name": macro, "body_sql": body}
        for key in ("name", "type", "widget", "default", "required", "constraint", "description"):
            data[f"arg_{key}"] = [a.get(key, "") for a in args or []]
        resp = self.client.post(f"/datamodules/{dm_uuid}/reports/new", data=data)
        assert resp.status_code == 302, resp.get_data(as_text=True)
        with self.app.app_context():
            from strata.models.datamodule import Datamodule
            from strata.models.report import Report

            dm = Datamodule.get_by_uuid(dm_uuid)
            assert dm is not None
            reports = [r for r in Report.get_for_datamodule(dm.id) if r.macro_name == macro]
            assert reports, self.flashes()
            return reports[0].uuid

    def new_api_key(self, dm_uuid: str, **form: str) -> str:
        data = {"name": "excel", "scope": "datamodule", **form}
        resp = self.client.post(f"/datamodules/{dm_uuid}/apikeys", data=data)
        assert resp.status_code == 302
        with self.client.session_transaction() as sess:
            revealed = sess.get("new_api_token")
        assert revealed, self.flashes()
        return revealed["token"]

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
