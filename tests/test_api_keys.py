"""API keys: anonymous data access for Excel/PowerQuery."""

import json

import pytest

from tests.conftest import Strata

ANON = {"X-Anon": "1"}


@pytest.fixture
def dm(strata: Strata) -> tuple[str, str]:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    rpt = strata.new_report(
        dm_uuid,
        body="SELECT * FROM sales WHERE region = $region",
        macro="by_region",
        args=[
            {"name": "region", "type": "text", "widget": "input", "default": "North"},
        ],
    )
    return dm_uuid, rpt


def test_anonymous_without_key_is_401(strata: Strata, dm: tuple[str, str]) -> None:
    _, rpt = dm
    assert strata.client.get(f"/r/{rpt}.csv", headers=ANON).status_code == 401


def test_anonymous_html_redirects_to_login(strata: Strata, dm: tuple[str, str]) -> None:
    _, rpt = dm
    resp = strata.client.get(f"/r/{rpt}.html", headers=ANON)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_valid_key_serves_data(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.json?key={token}&region=South", headers=ANON)
    assert resp.status_code == 200
    assert [r["product"] for r in json.loads(resp.get_data(as_text=True))] == ["B"]


def test_bad_token_is_403(strata: Strata, dm: tuple[str, str]) -> None:
    _, rpt = dm
    assert strata.client.get(f"/r/{rpt}.csv?key=nope", headers=ANON).status_code == 403


def test_fixed_args_win_over_caller(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid, fixed_args="region=North")
    resp = strata.client.get(f"/r/{rpt}.json?key={token}&region=South", headers=ANON)
    assert resp.status_code == 200
    assert {r["region"] for r in json.loads(resp.get_data(as_text=True))} == {"North"}


def test_report_scoped_key_is_refused_elsewhere(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    other = strata.new_report(
        dm_uuid, body="SELECT * FROM sales", name="Everything", macro="everything"
    )
    token = strata.new_api_key(dm_uuid, scope=f"report:{rpt}")
    assert strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON).status_code == 200
    assert strata.client.get(f"/r/{other}.csv?key={token}", headers=ANON).status_code == 403


def test_disabled_key_is_403(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    with strata.app.app_context():
        from strata.models.api_key import ApiKey, hash_token

        key = ApiKey.get_by_token_hash(hash_token(token))
        assert key is not None
        key_uuid = key.uuid
    assert strata.client.post(f"/datamodules/apikeys/{key_uuid}/toggle").status_code == 302
    assert strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON).status_code == 403


def test_keyed_requests_are_access_logged(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON)
    with strata.app.app_context():
        from strata.db import get_db

        rows = (
            get_db().execute("SELECT status, format, row_count FROM api_key_access_log").fetchall()
        )
    assert rows == [(200, "csv", 2)]  # region defaults to North: A and C
