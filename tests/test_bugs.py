"""Regression tests for the Phase A bug fixes."""

import io
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from tests.conftest import Strata


def test_tags_page_renders_with_a_tag(strata: Strata) -> None:
    resp = strata.client.post("/tags/new", data={"name": "finance", "color": "#3b82f6"})
    assert resp.status_code == 302
    page = strata.client.get("/tags/")
    assert page.status_code == 200
    assert "finance" in page.get_data(as_text=True)


@pytest.mark.parametrize(
    ("method", "url"),
    [("post", "/tags/new"), ("post", "/tags/1/edit"), ("post", "/tags/1/delete")],
)
def test_tag_changes_need_an_admin(strata: Strata, method: str, url: str) -> None:
    strata.client.post("/tags/new", data={"name": "finance", "color": "#3b82f6"})
    resp = getattr(strata.client, method)(
        url, data={"name": "x", "color": "#000000"}, headers={"X-User": "viewer"}
    )
    assert resp.status_code == 302
    assert "/tags" not in resp.headers["Location"]  # bounced to the index, not saved
    page = strata.client.get("/tags/").get_data(as_text=True)
    assert "finance" in page


@pytest.mark.parametrize("ttl", ["abc", "1.5", "-5"])
def test_bad_ttl_is_a_form_error_not_a_500(strata: Strata, ttl: str) -> None:
    resp = strata.client.post(
        "/datamodules/new", data={"path": "t/ttl", "build_script": "", "ttl_seconds": ttl}
    )
    assert resp.status_code == 200
    assert "TTL" in resp.get_data(as_text=True)
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={"path": "test/sales", "build_script": "", "ttl_seconds": ttl},
    )
    assert resp.status_code == 200
    assert "TTL" in resp.get_data(as_text=True)


@pytest.mark.parametrize("aware", [False, True])
def test_valid_until_without_a_timezone_is_utc(strata: Strata, aware: bool) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    past = datetime.now(UTC) - timedelta(hours=1)
    stamp = past.isoformat() if aware else past.replace(tzinfo=None).isoformat()
    with strata.app.app_context():
        from strata.db import transaction

        with transaction() as cur:
            cur.execute("UPDATE datamodule SET valid_until = ? WHERE uuid = ?", (stamp, dm_uuid))
    rpt = strata.new_report(dm_uuid)
    assert strata.client.get(f"/r/{rpt}.csv").status_code == 200
    assert len(strata.versions(dm_uuid)) == 2  # past the deadline: it rebuilt


def test_html_login_redirect_drops_the_api_key(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.html?key=SECRET&region=North", headers={"X-Anon": "1"})
    assert resp.status_code == 302
    assert "SECRET" not in resp.headers["Location"]
    next_url = parse_qs(urlparse(resp.headers["Location"]).query)["next"][0]
    assert parse_qs(urlparse(next_url).query) == {"region": ["North"]}


def test_upload_size_is_limited(strata: Strata) -> None:
    assert strata.app.config["MAX_CONTENT_LENGTH"] == 512 * 1024 * 1024  # server.max_upload_mb
    strata.app.config["MAX_CONTENT_LENGTH"] = 1024
    dm_uuid = strata.new_datamodule()
    resp = strata.client.post(
        f"/datamodules/{dm_uuid}/sources",
        data={"file": (io.BytesIO(b"x" * 4096), "big.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 413
