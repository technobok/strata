"""Excel links (X3/X4; one per datamodule since 2026-11-23): a report page's "Use in
Excel" box shows a link for the current view carrying its DATAMODULE's Excel key,
made the first time anyone opens a box on it. The key reads every report on the
datamodule and nothing else, belongs to the datamodule (not a person), and only its
owners and the admins replace it. Reports' old per-report keys keep working until
the datamodule's key is replaced.
"""

import hashlib
import html
import re
import secrets
import uuid
from urllib.parse import parse_qsl, urlsplit

import apsw
import pytest

from strata.models.api_key import ApiKey, hash_token
from strata.models.report import Report
from tests.conftest import Strata

VIEWER = {"X-User": "viewer"}
ANON = {"X-Anon": "1"}
LINK = re.compile(r'<option value="([^"]+)">([^<]+)</option>')


def _setup(strata: Strata) -> tuple[str, str, str]:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    report = strata.new_report(dm_uuid)
    other = strata.new_report(dm_uuid, name="Other")
    return dm_uuid, report, other


def _box(strata: Strata, report: str, query: str = "", headers: dict | None = None) -> str:
    resp = strata.client.get(f"/r/{report}/excel{query}", headers=headers or {})
    assert resp.status_code == 200, resp.status_code
    return html.unescape(resp.get_data(as_text=True))


def _links(box: str) -> dict[str, str]:
    """Format label -> link."""
    return {label: url for url, label in LINK.findall(box)}


def _local(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def _excel_key(strata: Strata, report: str) -> ApiKey | None:
    """The Excel link of the report's datamodule."""
    with strata.app.app_context():
        rpt = Report.get_by_uuid(report)
        assert rpt is not None
        return ApiKey.get_excel_link(rpt.datamodule_id)


def test_every_box_shows_the_key_made_with_the_datamodule(strata: Strata) -> None:
    _dm, report, other = _setup(strata)
    made = _excel_key(strata, report)  # made when the datamodule was
    assert made is not None and made.created_by == "tester"  # who made the datamodule
    box = _box(strata, report, headers=VIEWER)
    first = _links(box)
    assert set(first) == {
        "Excel workbook (.xlsx)",
        "CSV",
        "JSON",
        "Parquet (for Power BI and other tools; Excel cannot open it from a link)",
    }
    # xlsx is the default: Excel's From Web opens it as a table, and not Parquet
    shown = box.split('<code class="excel-url', 1)[1].split("</code>", 1)[0]
    assert f"/r/{report}.xlsx?key=" in shown
    again = _links(_box(strata, report))  # someone else, later: the same key
    assert again == first
    token = dict(parse_qsl(urlsplit(first["CSV"]).query))["key"]
    other_csv = _links(_box(strata, other))["CSV"]  # another report: the same key
    assert dict(parse_qsl(urlsplit(other_csv).query))["key"] == token

    key = _excel_key(strata, report)
    assert key is not None and key.excel_link and not key.old_report_link
    assert key.scope_type == "datamodule" and key.report_id is None
    assert key.id == made.id and key.name == "Excel link"
    assert key.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert key.token_encrypted and token not in key.token_encrypted
    with strata.app.app_context():
        rpt = Report.get_by_uuid(report)
        assert rpt is not None
        assert ApiKey.ensure_excel_link(rpt.datamodule_id, "someone").id == key.id  # one


def test_the_link_opens_this_view_and_its_key_reads_only_its_datamodule(
    strata: Strata,
) -> None:
    _dm, report, other = _setup(strata)
    elsewhere_dm = strata.new_datamodule(path="test/elsewhere")
    strata.build(elsewhere_dm)
    elsewhere = strata.new_report(elsewhere_dm)
    csv = _links(_box(strata, report, "?region=North&sort=-revenue"))["CSV"]
    assert urlsplit(csv).scheme == "http" and urlsplit(csv).path == f"/r/{report}.csv"
    resp = strata.client.get(_local(csv), headers=ANON)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).splitlines()
    assert len(body) == 3 and body[1].startswith("North,A,100")  # filtered, sorted
    token = dict(parse_qsl(urlsplit(csv).query))["key"]
    assert strata.client.get(f"/r/{other}.csv?key={token}", headers=ANON).status_code == 200
    assert strata.client.get(f"/r/{elsewhere}.csv?key={token}", headers=ANON).status_code == 403


def test_the_link_never_pins_a_page_or_version(strata: Strata) -> None:
    dm_uuid, report, _other = _setup(strata)
    [version] = strata.versions(dm_uuid)
    csv = _links(_box(strata, report, f"?q=North&page=2&version={version.uuid}&key=stolen"))["CSV"]
    pairs = parse_qsl(urlsplit(csv).query)
    assert [k for k, _v in pairs] == ["q", "key"] and ("key", "stolen") not in pairs


def test_the_report_page_offers_the_box_and_a_run_page_does_not(strata: Strata) -> None:
    _dm, report, _other = _setup(strata)
    page = strata.client.get(f"/r/{report}.html?region=North").get_data(as_text=True)
    assert f'hx-get="/r/{report}/excel?region=North"' in page and "Use in Excel" in page

    resp = strata.client.post(f"/reports/{report}/run")
    assert resp.status_code == 302
    run_page = strata.client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Use in Excel" not in run_page


def test_the_box_needs_a_login(strata: Strata) -> None:
    _dm, report, _other = _setup(strata)
    resp = strata.client.get(f"/r/{report}/excel", headers=ANON)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert b"key=" not in resp.get_data()


def test_only_the_datamodules_owners_replace_the_key(strata: Strata) -> None:
    _dm, report, other = _setup(strata)
    viewer_box = _box(strata, report, headers=VIEWER)
    assert "Replace the datamodule's Excel key" not in viewer_box
    old = _local(_links(viewer_box)["CSV"])
    resp = strata.client.post(f"/r/{report}/excel/replace", headers=VIEWER)
    assert resp.status_code == 403
    assert strata.client.get(old, headers=ANON).status_code == 200  # unchanged

    other_old = _local(_links(_box(strata, other))["CSV"])
    admin_box = _box(strata, report)
    assert "Replace the datamodule's Excel key" in admin_box
    replaced = strata.client.post(f"/r/{report}/excel/replace?region=North")
    assert replaced.status_code == 200
    replaced_box = html.unescape(replaced.get_data(as_text=True))
    assert "Replaced." in replaced_box and "region=North" in replaced_box
    new = _local(_links(replaced_box)["CSV"])
    assert strata.client.get(old, headers=ANON).status_code == 403
    assert strata.client.get(other_old, headers=ANON).status_code == 403  # every report
    assert strata.client.get(new, headers=ANON).status_code == 200

    # who made the report no longer matters: its datamodule's owners replace
    strata.set_access(_dm, owners="viewer")
    assert "Replace the datamodule's Excel key" in _box(strata, report, headers=VIEWER)
    assert strata.client.post(f"/r/{report}/excel/replace", headers=VIEWER).status_code == 200


def test_rotating_on_the_datamodule_page_updates_the_box(strata: Strata) -> None:
    _dm, report, _other = _setup(strata)
    before = _links(_box(strata, report))["CSV"]
    key = _excel_key(strata, report)
    assert key is not None
    assert strata.client.post(f"/datamodules/apikeys/{key.uuid}/rotate").status_code == 302
    after = _links(_box(strata, report))["CSV"]
    assert after != before
    assert strata.client.get(_local(after), headers=ANON).status_code == 200


def test_admins_see_every_key(strata: Strata) -> None:
    dm_uuid, report, _other = _setup(strata)
    strata.new_api_key(dm_uuid, name="shared workbook")
    _box(strata, report, headers=VIEWER)
    page = strata.client.get("/admin/keys").get_data(as_text=True)
    assert "shared workbook" in page and "Excel link" in page
    assert page.count("test/sales</a> (every report)") == 2
    assert strata.client.get("/admin/keys", headers=VIEWER).status_code == 302


@pytest.mark.parametrize(
    ("scope", "encrypted", "on_datamodule"),
    [
        ("report", "x", False),  # a datamodule's Excel link is datamodule-scoped
        ("datamodule", None, True),  # and keeps its token encrypted
        ("datamodule", "x", False),  # and names its datamodule
    ],
)
def test_the_schema_holds_an_excel_link_to_its_shape(
    strata: Strata, scope: str, encrypted: str | None, on_datamodule: bool
) -> None:
    dm_uuid, _report, _other = _setup(strata)  # a real datamodule: only the CHECK refuses
    with strata.app.app_context():
        from strata.db import get_db

        dm = strata.datamodule(dm_uuid)
        with pytest.raises(apsw.ConstraintError, match="CHECK"):
            get_db().execute(
                "INSERT INTO api_key (uuid, token_hash, token_prefix, name, scope_type, "
                "datamodule_id, created_by, created_at, token_encrypted, "
                "datamodule_excel_link) VALUES ('u', 'h', 'p', 'n', ?, ?, 'jo', 'now', ?, 1)",
                (scope, dm.id if on_datamodule else None, encrypted),
            )


def test_a_racing_second_open_gets_the_same_key(
    strata: Strata, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people open a box at once: both find no key; the one-per-datamodule index
    turns the second insert into a read of the first key."""
    _dm, report, _other = _setup(strata)
    _box(strata, report)
    first = _excel_key(strata, report)
    assert first is not None
    real = ApiKey.get_excel_link
    seen: list[int] = []

    def read_before_the_first_commit(datamodule_id: int) -> ApiKey | None:
        seen.append(datamodule_id)
        return None if len(seen) == 1 else real(datamodule_id)

    monkeypatch.setattr(ApiKey, "get_excel_link", staticmethod(read_before_the_first_commit))
    with strata.app.app_context():
        assert ApiKey.ensure_excel_link(first.datamodule_id or 0, "someone").id == first.id
        assert len([k for k in ApiKey.get_all() if k.excel_link]) == 1


def test_an_admin_keys_links_are_xlsx(strata: Strata) -> None:
    """The datamodule page's links for a new key open in Excel's From Web (not Parquet)."""
    dm_uuid, report, _other = _setup(strata)
    token = strata.new_api_key(dm_uuid)
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert f"/r/{report}.xlsx?key={token}" in page and ".parquet?key=" not in page


def _old_report_link(strata: Strata, report: str) -> str:
    """A report's Excel link as made before 2026-11-23 (one per report)."""
    from strata.crypto import encrypt_text
    from strata.db import get_db

    token = secrets.token_urlsafe(32)
    with strata.app.app_context():
        rpt = Report.get_by_uuid(report)
        assert rpt is not None
        get_db().execute(
            "INSERT INTO api_key (uuid, token_hash, token_prefix, name, scope_type, report_id, "
            "created_by, created_at, token_encrypted, excel_link) "
            "VALUES (?, ?, ?, 'Excel link (one report, before 2026-11-23)', 'report', ?, "
            "'jo', 'now', ?, 1)",
            (str(uuid.uuid4()), hash_token(token), token[:8], rpt.id, encrypt_text(token)),
        )
    return token


def test_an_old_report_link_works_until_the_datamodules_key_is_replaced(
    strata: Strata,
) -> None:
    _dm, report, other = _setup(strata)
    old = _old_report_link(strata, report)
    assert strata.client.get(f"/r/{report}.csv?key={old}", headers=ANON).status_code == 200
    box = _box(strata, report)
    assert old not in box  # the box shows the datamodule's key, not the old one
    page = strata.client.get(f"/datamodules/{_dm}/edit").get_data(as_text=True)
    assert f"/r/{report}.xlsx?key={old}" in page  # its owners still see where it reads
    assert f"/r/{other}.xlsx?key={old}" not in page
    assert strata.client.get(f"/r/{other}.csv?key={old}", headers=ANON).status_code == 403
    assert strata.client.post(f"/r/{report}/excel/replace").status_code == 200
    assert strata.client.get(f"/r/{report}.csv?key={old}", headers=ANON).status_code == 403


def test_rotating_the_datamodules_key_on_its_page_disables_old_links_too(
    strata: Strata,
) -> None:
    _dm, report, _other = _setup(strata)
    old = _old_report_link(strata, report)
    _box(strata, report)
    key = _excel_key(strata, report)
    assert key is not None
    assert strata.client.post(f"/datamodules/apikeys/{key.uuid}/rotate").status_code == 302
    assert strata.client.get(f"/r/{report}.csv?key={old}", headers=ANON).status_code == 403


def test_0011_keeps_old_report_links_and_names_them(tmp_path, monkeypatch) -> None:
    from strata import db

    path = str(tmp_path / "m.sqlite3")
    every = db._migration_files
    monkeypatch.setattr(db, "_migration_files", lambda: [f for f in every() if f[0] <= 10])
    db.init_db_at(path)
    conn = apsw.Connection(path)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO datamodule (uuid, path, build_script, created_by, modified_by, "
        "created_at, modified_at) VALUES ('d1', 't/x', '', 'jo', 'jo', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO report (uuid, datamodule_id, name, macro_name, kind, created_by, "
        "modified_by, created_at, modified_at) VALUES ('r1', 1, 'R', 'r', 'query', 'jo', "
        "'jo', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO api_key (uuid, token_hash, token_prefix, name, scope_type, report_id, "
        "created_by, created_at, token_encrypted, excel_link) "
        "VALUES ('k1', 'h1', 'p1', 'Excel link', 'report', 1, 'jo', ?, 'enc', 1)",
        (now,),
    )
    conn.close()
    monkeypatch.setattr(db, "_migration_files", every)
    db.init_db_at(path)
    conn = apsw.Connection(path)
    assert conn.execute(
        "SELECT name, scope_type, enabled, excel_link, datamodule_excel_link FROM api_key"
    ).fetchall() == [("Excel link (one report, before 2026-11-23)", "report", 1, 1, 0)]
    conn.close()


def test_a_new_datamodule_has_its_excel_key_before_any_report(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    with strata.app.app_context():
        from strata.models.datamodule import Datamodule

        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        key = ApiKey.get_excel_link(dm.id)
    assert key is not None and key.excel_link and key.scope_type == "datamodule"
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert key.token_prefix in page  # listed in its API keys table


def test_an_older_datamodule_without_a_key_gets_one_on_its_page(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    with strata.app.app_context():
        from strata.db import transaction
        from strata.models.datamodule import Datamodule

        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        with transaction() as cur:  # as made before keys came with the datamodule
            cur.execute("DELETE FROM api_key WHERE datamodule_id = ?", (dm.id,))
        assert ApiKey.get_excel_link(dm.id) is None
    strata.client.get(f"/datamodules/{dm_uuid}/edit")  # no reports yet
    with strata.app.app_context():
        assert ApiKey.get_excel_link(dm.id) is not None
