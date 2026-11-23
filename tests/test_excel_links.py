"""Each report's Excel link (X3/X4): the report page's "Use in Excel" box shows a
link for the current view carrying the report's key, made the first time anyone
opens the box. The key reads that report only, belongs to the report (not a
person), and only an admin or the report's owner can replace it.
"""

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlsplit

import apsw
import pytest

from strata.models.api_key import ApiKey
from strata.models.report import Report
from tests.conftest import Strata

VIEWER = {"X-User": "viewer"}
ANON = {"X-Anon": "1"}
LINK = re.compile(r'<option value="([^"]+)">([^<]+)</option>')


def _setup(strata: Strata) -> tuple[str, str, str]:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    report = strata.new_report(dm_uuid)
    other = strata.new_report(dm_uuid, name="Other", macro="other_report")
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
    with strata.app.app_context():
        rpt = Report.get_by_uuid(report)
        assert rpt is not None
        return ApiKey.get_excel_link(rpt.id)


def test_opening_the_box_makes_the_reports_key_once(strata: Strata) -> None:
    _dm, report, _other = _setup(strata)
    assert _excel_key(strata, report) is None  # nobody opened the box yet
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

    key = _excel_key(strata, report)
    assert key is not None and key.excel_link and key.scope_type == "report"
    assert key.created_by == "viewer" and key.name == "Excel link"
    token = dict(parse_qsl(urlsplit(first["CSV"]).query))["key"]
    assert key.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert key.token_encrypted and token not in key.token_encrypted
    with strata.app.app_context():
        rpt = Report.get_by_uuid(report)
        assert rpt is not None
        assert ApiKey.ensure_excel_link(rpt.id, "someone").id == key.id  # never a second


def test_the_link_reads_this_report_only_in_this_view(strata: Strata) -> None:
    _dm, report, other = _setup(strata)
    csv = _links(_box(strata, report, "?region=North&sort=-revenue"))["CSV"]
    assert urlsplit(csv).scheme == "http" and urlsplit(csv).path == f"/r/{report}.csv"
    resp = strata.client.get(_local(csv), headers=ANON)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).splitlines()
    assert len(body) == 3 and body[1].startswith("North,A,100")  # filtered, sorted
    token = dict(parse_qsl(urlsplit(csv).query))["key"]
    assert strata.client.get(f"/r/{other}.csv?key={token}", headers=ANON).status_code == 403


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
    assert _excel_key(strata, report) is None  # the page alone makes no key

    resp = strata.client.post(f"/reports/{report}/run")
    assert resp.status_code == 302
    run_page = strata.client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Use in Excel" not in run_page


def test_the_box_needs_a_login(strata: Strata) -> None:
    _dm, report, _other = _setup(strata)
    resp = strata.client.get(f"/r/{report}/excel", headers=ANON)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert _excel_key(strata, report) is None


def test_only_the_datamodules_owners_replace_the_key(strata: Strata) -> None:
    _dm, report, _other = _setup(strata)
    viewer_box = _box(strata, report, headers=VIEWER)
    assert "Replace this report's key" not in viewer_box
    old = _local(_links(viewer_box)["CSV"])
    resp = strata.client.post(f"/r/{report}/excel/replace", headers=VIEWER)
    assert resp.status_code == 403
    assert strata.client.get(old, headers=ANON).status_code == 200  # unchanged

    admin_box = _box(strata, report)
    assert "Replace this report's key" in admin_box
    replaced = strata.client.post(f"/r/{report}/excel/replace?region=North")
    assert replaced.status_code == 200
    replaced_box = html.unescape(replaced.get_data(as_text=True))
    assert "Replaced." in replaced_box and "region=North" in replaced_box
    new = _local(_links(replaced_box)["CSV"])
    assert strata.client.get(old, headers=ANON).status_code == 403
    assert strata.client.get(new, headers=ANON).status_code == 200

    # who made the report no longer matters: its datamodule's owners replace
    strata.set_access(_dm, owners="viewer")
    assert "Replace this report's key" in _box(strata, report, headers=VIEWER)
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
    assert "test/sales</a>: Sales" in page and "(every report)" in page
    assert strata.client.get("/admin/keys", headers=VIEWER).status_code == 302


@pytest.mark.parametrize(
    ("scope", "encrypted", "on_report"),
    [
        ("datamodule", "x", False),  # an Excel link is report-scoped
        ("report", None, True),  # and keeps its token encrypted
    ],
)
def test_the_schema_holds_an_excel_link_to_its_shape(
    strata: Strata, scope: str, encrypted: str | None, on_report: bool
) -> None:
    _dm, report, _other = _setup(strata)  # a real report: only the CHECK may refuse
    with strata.app.app_context():
        from strata.db import get_db

        rpt = Report.get_by_uuid(report)
        assert rpt is not None
        with pytest.raises(apsw.ConstraintError, match="CHECK"):
            get_db().execute(
                "INSERT INTO api_key (uuid, token_hash, token_prefix, name, scope_type, "
                "report_id, created_by, created_at, token_encrypted, excel_link) "
                "VALUES ('u', 'h', 'p', 'n', ?, ?, 'jo', 'now', ?, 1)",
                (scope, rpt.id if on_report else None, encrypted),
            )


def test_a_racing_second_open_gets_the_same_key(
    strata: Strata, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people open the box at once: both find no key; the one-per-report index
    turns the second insert into a read of the first key."""
    _dm, report, _other = _setup(strata)
    _box(strata, report)
    first = _excel_key(strata, report)
    assert first is not None
    real = ApiKey.get_excel_link
    seen: list[int] = []

    def read_before_the_first_commit(report_id: int) -> ApiKey | None:
        seen.append(report_id)
        return None if len(seen) == 1 else real(report_id)

    monkeypatch.setattr(ApiKey, "get_excel_link", staticmethod(read_before_the_first_commit))
    with strata.app.app_context():
        assert ApiKey.ensure_excel_link(first.report_id or 0, "someone").id == first.id
        assert len([k for k in ApiKey.get_all() if k.excel_link]) == 1


def test_an_admin_keys_links_are_xlsx(strata: Strata) -> None:
    """The datamodule page's links for a new key open in Excel's From Web (not Parquet)."""
    dm_uuid, report, _other = _setup(strata)
    token = strata.new_api_key(dm_uuid)
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert f"/r/{report}.xlsx?key={token}" in page and ".parquet?key=" not in page
