"""The read audit: one read_log row per read of a datamodule's data — report pages
and data, runs, Excel keys shown, script previews, version scripts and source
files — by a person or a key, refusals included; browsable by admins (everything)
and by a datamodule's owners (its reads), with a CSV download; pruned after
audit.retention_days.
"""

import csv
import io
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from strata.models.read_log import ReadFilter, ReadLog
from tests.conftest import Strata, as_user

OWNER = as_user("alice")
VIEWER = as_user("vera", "brs-finance")
OUTSIDER = as_user("olly")
ANON = {"X-Anon": "1"}


def _reads(strata: Strata) -> list[ReadLog]:
    """Every row, oldest first."""
    with strata.app.app_context():
        return list(reversed(list(ReadLog.iterate(ReadFilter()))))


def _clear(strata: Strata) -> None:
    from strata.db import transaction

    with strata.app.app_context(), transaction() as cur:
        cur.execute("DELETE FROM read_log")


@pytest.fixture
def sales(strata: Strata) -> tuple[str, str]:
    """A built datamodule owned by alice, viewed by @brs-finance, with a report that
    takes a region (default North)."""
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE region = $region",
        name="By region",
        args=[
            {
                "name": "region",
                "type": "text",
                "widget": "input",
                "default": "North",
                "required": "no",
            }
        ],
    )
    assert strata.set_access(dm_uuid, owners="alice", viewers="@brs-finance").status_code == 302
    _clear(strata)
    return dm_uuid, rpt


# ---------------------------------------------------------------------------
# What is recorded
# ---------------------------------------------------------------------------


def test_a_report_page_and_its_fragments(strata: Strata, sales: tuple[str, str]) -> None:
    _dm, rpt = sales
    assert strata.client.get(f"/r/{rpt}.html?region=South", headers=VIEWER).status_code == 200
    fragment = {**VIEWER, "HX-Request": "true"}
    assert (
        strata.client.get(f"/r/{rpt}.html?region=North&sort=-revenue", headers=fragment).status_code
        == 200
    )
    page, sort = _reads(strata)
    assert (page.kind, page.username, page.status, page.row_count) == ("page", "vera", 200, 1)
    assert page.bytes_sent is None  # a page's size is not what was read
    assert (page.report_name, page.datamodule_path, page.format) == (
        "By region",
        "test/sales",
        "html",
    )
    assert page.query == "region=South" and page.version_uuid
    assert (sort.query, sort.row_count) == ("region=North&sort=-revenue", 2)
    assert page.remote_addr == "127.0.0.1" and page.duration_ms is not None


def test_report_data_by_a_person(strata: Strata, sales: tuple[str, str]) -> None:
    _dm, rpt = sales
    resp = strata.client.get(f"/r/{rpt}.csv", headers=VIEWER)
    assert resp.status_code == 200
    [row] = _reads(strata)
    assert (row.kind, row.username, row.format, row.row_count) == ("data", "vera", "csv", 2)
    assert row.bytes_sent == int(resp.headers["Content-Length"]) and row.api_key_id is None


def test_report_data_by_a_key(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, rpt = sales
    token = strata.new_api_key(dm_uuid, name="finance workbook")
    headers = {
        **ANON,
        "User-Agent": "Microsoft.Data.Mashup (https://go.microsoft.com/fwlink/?LinkID=304225)",
    }
    assert (
        strata.client.get(f"/r/{rpt}.xlsx?key={token}&region=South", headers=headers).status_code
        == 200
    )
    [row] = _reads(strata)
    assert (row.username, row.key_name, row.token_prefix) == (None, "finance workbook", token[:8])
    assert row.query == "region=South" and token not in (row.query or "")  # never the key
    assert row.user_agent and row.user_agent.startswith("Microsoft.Data.Mashup")
    with strata.app.app_context():
        from strata.models.api_key import ApiKey

        key = ApiKey.get_by_id(row.api_key_id or 0)
        assert key is not None and key.use_count == 1  # Uses / Last used still count


def test_refusals_are_recorded(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, rpt = sales
    token = strata.new_api_key(dm_uuid)
    _clear(strata)
    assert strata.client.get(f"/r/{rpt}.html", headers=OUTSIDER).status_code == 403
    assert strata.client.get(f"/r/{rpt}.csv", headers=ANON).status_code == 401
    assert strata.client.get(f"/r/{rpt}.csv?key=nosuchtoken123", headers=ANON).status_code == 403
    assert strata.client.get(f"/r/{rpt}.csv?key={token}&nosuch=1", headers=ANON).status_code == 400
    assert strata.client.get(f"/r/{rpt}.pdf", headers=VIEWER).status_code == 400
    outsider, no_key, unknown, bad_arg, bad_ext = _reads(strata)
    assert (outsider.username, outsider.status, outsider.kind) == ("olly", 403, "page")
    assert (no_key.username, no_key.token_prefix, no_key.status) == (None, None, 401)
    assert (unknown.api_key_id, unknown.token_prefix, unknown.status) == (None, "nosuchto", 403)
    assert (bad_arg.token_prefix, bad_arg.status) == (token[:8], 400)
    assert (bad_ext.format, bad_ext.status) == ("pdf", 400)
    assert all(r.refused for r in (outsider, no_key, unknown, bad_arg, bad_ext))


def test_a_disabled_key_is_recorded_by_name(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, rpt = sales
    token = strata.new_api_key(dm_uuid, name="old sheet")
    with strata.app.app_context():
        from strata.models.api_key import ApiKey, hash_token

        key = ApiKey.get_by_token_hash(hash_token(token))
        assert key is not None
    strata.client.post(f"/datamodules/apikeys/{key.uuid}/toggle")
    _clear(strata)
    assert strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON).status_code == 403
    [row] = _reads(strata)
    assert (row.key_name, row.api_key_id, row.status) == ("old sheet", key.id, 403)


def test_a_locked_datamodule_refusal(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, rpt = sales
    from strata.db import transaction

    with strata.app.app_context(), transaction() as cur:
        cur.execute("UPDATE datamodule SET access_lock = 'test' WHERE uuid = ?", (dm_uuid,))
    assert strata.client.get(f"/r/{rpt}.csv", headers=VIEWER).status_code == 403
    [row] = _reads(strata)
    assert (row.username, row.status, row.kind) == ("vera", 403, "data")


def test_a_redirect_to_sign_in_is_not_a_read(strata: Strata, sales: tuple[str, str]) -> None:
    _dm, rpt = sales
    assert strata.client.get(f"/r/{rpt}.html", headers=ANON).status_code == 302
    assert _reads(strata) == []


def test_runs_their_pages_and_downloads(strata: Strata, sales: tuple[str, str]) -> None:
    _dm, rpt = sales
    made = strata.client.post(f"/reports/{rpt}/run", data={"region": "South"}, headers=OWNER)
    assert made.status_code == 302
    run_url = made.headers["Location"]
    run_uuid = run_url.rsplit("/", 1)[1]
    assert strata.client.get(run_url, headers=VIEWER).status_code == 200
    assert strata.client.get(f"/runs/{run_uuid}.csv", headers=VIEWER).status_code == 200
    assert strata.client.get(run_url, headers=OUTSIDER).status_code == 403
    page, download, refused = _reads(strata)  # making the run is not a read: its page is
    assert (page.kind, page.run_uuid, page.row_count) == ("run", run_uuid, 1)
    assert (download.kind, download.format, download.row_count) == ("run-data", "csv", 1)
    assert (refused.username, refused.status) == ("olly", 403)


def test_the_excel_box_shows_a_key(strata: Strata, sales: tuple[str, str]) -> None:
    _dm, rpt = sales
    assert strata.client.get(f"/r/{rpt}/excel", headers=VIEWER).status_code == 200
    assert strata.client.get(f"/r/{rpt}/excel", headers=OUTSIDER).status_code == 403
    shown, refused = _reads(strata)
    assert (shown.kind, shown.username, shown.key_name) == ("excel-key", "vera", "Excel link")
    assert (refused.username, refused.status, refused.api_key_id) == ("olly", 403, None)


def test_previews_scripts_and_sources(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, _rpt = sales
    strata.upload_source(dm_uuid, "payroll.csv", b"name,pay\nA,1\n")
    _clear(strata)
    with strata.app.app_context():
        from strata.models.source_file import SourceFile

        [source] = SourceFile.get_for_datamodule(strata.datamodule(dm_uuid).id)
    strata.client.get(f"/datamodules/{dm_uuid}/sources/{source.id}/download", headers=OWNER)
    [version] = strata.versions(dm_uuid)
    strata.client.get(f"/datamodules/{dm_uuid}/versions/{version.uuid}/script", headers=OWNER)
    strata.client.post(
        f"/datamodules/{dm_uuid}/preview-script",
        data={"build_script": "SELECT {{ 1 }}"},
        headers=OWNER,
    )
    source_read, script, preview = _reads(strata)
    assert (source_read.kind, source_read.query, source_read.username) == (
        "source",
        "file=payroll.csv",
        "alice",
    )
    assert (script.kind, script.version_uuid) == ("script", version.uuid)
    assert preview.kind == "preview" and preview.status == 200


def test_rows_outlive_what_they_name(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, rpt = sales
    token = strata.new_api_key(dm_uuid, name="gone soon")
    strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON)
    strata.client.post(f"/datamodules/reports/{rpt}/delete")
    strata.client.post(f"/datamodules/{dm_uuid}/delete")
    [row] = _reads(strata)
    assert (row.key_name, row.report_name, row.datamodule_path) == (
        "gone soon",
        "By region",
        "test/sales",
    )


def test_a_failed_write_never_breaks_the_response(
    strata: Strata, sales: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _dm, rpt = sales

    def broken(**_values: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(ReadLog, "record", staticmethod(broken))
    assert strata.client.get(f"/r/{rpt}.csv", headers=VIEWER).status_code == 200


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


def _page_rows(html: str) -> int:
    return len(re.findall(r'<span class="read-kind">', html))


def test_the_admin_page_lists_and_filters(strata: Strata, sales: tuple[str, str]) -> None:
    _dm, rpt = sales
    strata.client.get(f"/r/{rpt}.html", headers=VIEWER)
    strata.client.get(f"/r/{rpt}.csv", headers=VIEWER)
    strata.client.get(f"/r/{rpt}.html", headers=OUTSIDER)
    page = strata.client.get("/admin/reads").get_data(as_text=True)
    assert _page_rows(page) == 3 and "test/sales" in page and "By region" in page
    assert _page_rows(strata.client.get("/admin/reads?kind=data").get_data(as_text=True)) == 1
    assert _page_rows(strata.client.get("/admin/reads?user=VERA").get_data(as_text=True)) == 2
    refused = strata.client.get("/admin/reads?refused=1").get_data(as_text=True)
    assert _page_rows(refused) == 1 and "olly" in refused
    today = datetime.now(UTC).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    assert (
        _page_rows(strata.client.get(f"/admin/reads?from={tomorrow}").get_data(as_text=True)) == 0
    )
    assert _page_rows(strata.client.get(f"/admin/reads?to={today}").get_data(as_text=True)) == 3
    bad = strata.client.get("/admin/reads?from=yesterday").get_data(as_text=True)
    assert "From &#39;yesterday&#39; is not a date" in bad and _page_rows(bad) == 3


def test_the_admin_page_pages_by_older(
    strata: Strata, sales: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import strata.models.read_log as read_log

    _dm, rpt = sales
    for region in ("North", "South", "East"):
        strata.client.get(f"/r/{rpt}.html?region={region}", headers=VIEWER)
    monkeypatch.setattr(read_log, "PAGE_SIZE", 2)
    first = strata.client.get("/admin/reads?kind=page").get_data(as_text=True)
    assert _page_rows(first) == 2 and "region=East" in first and "region=North" not in first
    older = re.search(r'href="([^"]*before=\d+[^"]*)">Older', first)
    assert older and "kind=page" in older.group(1)
    second = strata.client.get(older.group(1).replace("&amp;", "&")).get_data(as_text=True)
    assert _page_rows(second) == 1 and "region=North" in second and "Newest" in second


def test_the_csv_has_every_matching_row(strata: Strata, sales: tuple[str, str]) -> None:
    _dm, rpt = sales
    strata.client.get(f"/r/{rpt}.html", headers=VIEWER)
    strata.client.get(f"/r/{rpt}.csv", headers=VIEWER)
    resp = strata.client.get("/admin/reads.csv?kind=data")
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
    assert [(r["kind"], r["username"], r["format"]) for r in rows] == [("data", "vera", "csv")]
    assert list(rows[0]) == list(ReadLog.FIELDS)


def test_the_admin_pages_are_for_admins(strata: Strata, sales: tuple[str, str]) -> None:
    for url in ("/admin/reads", "/admin/reads.csv"):
        assert strata.client.get(url, headers=OWNER).status_code == 302  # to the home page


def test_owners_see_their_datamodules_reads_only(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, rpt = sales
    other = strata.new_datamodule(path="test/other")
    strata.build(other)
    other_rpt = strata.new_report(other, name="Other")
    strata.client.get(f"/r/{rpt}.html", headers=VIEWER)
    strata.client.get(f"/r/{other_rpt}.html")
    other_id = strata.datamodule(other).id
    page = strata.client.get(f"/datamodules/{dm_uuid}/reads?dm={other_id}", headers=OWNER).get_data(
        as_text=True
    )
    assert _page_rows(page) == 1 and "By region" in page and "Other" not in page
    csv_text = strata.client.get(f"/datamodules/{dm_uuid}/reads.csv", headers=OWNER).get_data(
        as_text=True
    )
    assert "By region" in csv_text and "Other" not in csv_text
    assert strata.client.get(f"/datamodules/{dm_uuid}/reads", headers=VIEWER).status_code == 403
    edit = strata.client.get(f"/datamodules/{dm_uuid}/edit", headers=OWNER).get_data(as_text=True)
    assert f'href="/datamodules/{dm_uuid}/reads"' in edit


def test_a_keys_reads(strata: Strata, sales: tuple[str, str]) -> None:
    dm_uuid, rpt = sales
    token = strata.new_api_key(dm_uuid, name="finance workbook")
    strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON)
    strata.client.get(f"/r/{rpt}.csv", headers=VIEWER)
    with strata.app.app_context():
        from strata.models.api_key import ApiKey, hash_token

        key = ApiKey.get_by_token_hash(hash_token(token))
        assert key is not None
    keys_page = strata.client.get("/admin/keys").get_data(as_text=True)
    assert f'href="/admin/reads?key={key.id}"' in keys_page
    page = strata.client.get(f"/admin/reads?key={key.id}").get_data(as_text=True)
    assert _page_rows(page) == 1 and "key: finance workbook" in page


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _age_all(strata: Strata, days: int) -> None:
    from strata.db import transaction

    when = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with strata.app.app_context(), transaction() as cur:
        cur.execute("UPDATE read_log SET at = ?", (when,))


def test_rows_past_the_retention_are_pruned(strata: Strata, sales: tuple[str, str]) -> None:
    from strata.services import datamodule_service, read_audit

    _dm, rpt = sales
    strata.client.get(f"/r/{rpt}.csv", headers=VIEWER)
    _age_all(strata, 366)
    strata.client.get(f"/r/{rpt}.html", headers=VIEWER)  # today's
    with strata.app.app_context():
        assert datamodule_service.reconcile(dry_run=True).expired_reads == 1
        assert len(_reads(strata)) == 2
        assert read_audit.prune() == 1
    assert [r.kind for r in _reads(strata)] == ["page"]


def test_zero_keeps_every_row(strata: Strata, sales: tuple[str, str]) -> None:
    from strata.services import read_audit

    _dm, rpt = sales
    strata.client.get(f"/r/{rpt}.csv", headers=VIEWER)
    _age_all(strata, 5000)
    strata.set_config({"audit.retention_days": 0})
    with strata.app.app_context():
        assert read_audit.prune() == 0
    assert len(_reads(strata)) == 1
