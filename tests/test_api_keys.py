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


def test_a_key_reads_every_report_of_its_datamodule_only(
    strata: Strata, dm: tuple[str, str]
) -> None:
    """User decision 2026-11-23: no scope; a key is for the whole datamodule."""
    dm_uuid, rpt = dm
    other = strata.new_report(dm_uuid, body="SELECT * FROM sales", name="Everything")
    elsewhere_dm = strata.new_datamodule(path="test/elsewhere")
    strata.build(elsewhere_dm)
    elsewhere = strata.new_report(elsewhere_dm)
    token = strata.new_api_key(dm_uuid, scope=f"report:{rpt}")  # an old form's scope: ignored
    assert strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON).status_code == 200
    assert strata.client.get(f"/r/{other}.csv?key={token}", headers=ANON).status_code == 200
    assert strata.client.get(f"/r/{elsewhere}.csv?key={token}", headers=ANON).status_code == 403


def _old_key(strata: Strata, rpt: str | None, dm_uuid: str | None = None) -> str:
    """A key as made before 2026-11-23: only its hash kept; report-scoped if ``rpt``."""
    import secrets
    import uuid

    from strata.db import get_db
    from strata.models.api_key import hash_token
    from strata.models.report import Report

    token = secrets.token_urlsafe(32)
    with strata.app.app_context():
        report_id = Report.get_by_uuid(rpt).id if rpt else None  # type: ignore[union-attr]
        dm_id = strata.datamodule(dm_uuid).id if dm_uuid else None
        get_db().execute(
            "INSERT INTO api_key (uuid, token_hash, token_prefix, name, scope_type, "
            "datamodule_id, report_id, created_by, created_at) "
            "VALUES (?, ?, ?, 'old key', ?, ?, ?, 'jo', 'now')",
            (
                str(uuid.uuid4()),
                hash_token(token),
                token[:8],
                "report" if rpt else "datamodule",
                dm_id,
                report_id,
            ),
        )
    return token


def test_an_old_report_scoped_key_still_reads_its_report_only(
    strata: Strata, dm: tuple[str, str]
) -> None:
    dm_uuid, rpt = dm
    other = strata.new_report(dm_uuid, body="SELECT * FROM sales", name="Everything")
    token = _old_key(strata, rpt)
    assert strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON).status_code == 200
    assert strata.client.get(f"/r/{other}.csv?key={token}", headers=ANON).status_code == 403


def _page(strata: Strata, dm_uuid: str) -> str:
    return strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)


def test_a_keys_links_stay_on_its_datamodule_page(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid, name="finance workbook")
    link = f"http://localhost/r/{rpt}.xlsx?region=North&amp;key={token}"  # its default
    copy = f'data-copy="{link}" title="{link}">Copy: Sales</button>'
    assert copy in _page(strata, dm_uuid)
    assert copy in _page(strata, dm_uuid)  # not once: every time
    named = _page(strata, dm_uuid).split('id="named-keys"')[1]
    assert "<details" not in named.split("<tbody>")[1].split("</table>")[0]  # no nested box
    assert "Scope" not in _page(strata, dm_uuid)  # the form offers no scope


def test_an_old_key_says_to_rotate_and_then_shows_its_links(
    strata: Strata, dm: tuple[str, str]
) -> None:
    dm_uuid, rpt = dm
    old = _old_key(strata, None, dm_uuid)
    assert "Rotate to see its links" in _page(strata, dm_uuid)
    with strata.app.app_context():
        from strata.models.api_key import ApiKey, hash_token

        key = ApiKey.get_by_token_hash(hash_token(old))
        assert key is not None
    assert strata.client.post(f"/datamodules/apikeys/{key.uuid}/rotate").status_code == 302
    page = _page(strata, dm_uuid)
    assert "Rotate to see its links" not in page
    with strata.app.app_context():
        rotated = ApiKey.get_by_uuid(key.uuid)
        assert rotated is not None
        assert f"/r/{rpt}.xlsx?region=North&amp;key={rotated.link_token()}" in page


def test_the_reports_table_has_each_reports_excel_link(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    other = strata.new_report(dm_uuid, body="SELECT * FROM sales", name="Everything")
    page = _page(strata, dm_uuid)
    with strata.app.app_context():
        from strata.models.api_key import ApiKey

        excel = ApiKey.get_excel_link(strata.datamodule(dm_uuid).id)
        assert excel is not None  # made for the page, and listed with the keys
        token = excel.link_token()
    # every argument is in the link, with its default, so it can be changed in Excel
    for report, query in ((rpt, f"region=North&key={token}"), (other, f"key={token}")):
        link = f"/r/{report}.xlsx?{query}"
        shown = link.replace("&", "&amp;")
        assert f'class="outline small excel-link" data-copy="http://localhost{shown}"' in page
        assert strata.client.get(link, headers=ANON).status_code == 200
    # the Excel link key has its own line (Replace, Disable, no Delete), not a Named keys row
    line = page.split('id="excel-link"')[1].split("</div>\n</div>")[0]
    assert excel.token_prefix in line and ">Replace<" in line and ">Disable<" in line
    assert ">Delete<" not in line
    assert ">Excel link</td>" not in page


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


def test_keyed_requests_are_in_the_read_audit(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    strata.client.get(f"/r/{rpt}.csv?key={token}", headers=ANON)
    with strata.app.app_context():
        from strata.db import get_db

        rows = (
            get_db()
            .execute("SELECT kind, status, format, row_count, token_prefix FROM read_log")
            .fetchall()
        )
    assert rows == [("data", 200, "csv", 2, token[:8])]  # region defaults to North: A and C


def _bearer(token: str, scheme: str = "Bearer") -> dict[str, str]:
    return {**ANON, "Authorization": f"{scheme} {token}"}


@pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
def test_a_key_in_the_authorization_header_serves_data(
    strata: Strata, dm: tuple[str, str], scheme: str
) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.json?region=South", headers=_bearer(token, scheme))
    assert resp.status_code == 200
    assert [r["product"] for r in json.loads(resp.get_data(as_text=True))] == ["B"]


def test_the_same_key_in_the_link_and_the_header_serves_data(
    strata: Strata, dm: tuple[str, str]
) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.csv?key={token}", headers=_bearer(token))
    assert resp.status_code == 200


def test_two_different_keys_are_refused(strata: Strata, dm: tuple[str, str]) -> None:
    from strata.blueprints.reports import TWO_KEYS

    dm_uuid, rpt = dm
    in_link, in_header = strata.new_api_key(dm_uuid), strata.new_api_key(dm_uuid)
    for token in (in_link, in_header):  # each key works on its own
        assert strata.client.get(f"/r/{rpt}.csv", headers=_bearer(token)).status_code == 200
    resp = strata.client.get(f"/r/{rpt}.csv?key={in_link}", headers=_bearer(in_header))
    assert resp.status_code == 400
    assert resp.get_data(as_text=True) == TWO_KEYS


def test_a_wrong_key_in_the_header_is_403(strata: Strata, dm: tuple[str, str]) -> None:
    _, rpt = dm
    assert strata.client.get(f"/r/{rpt}.csv", headers=_bearer("nope")).status_code == 403


@pytest.mark.parametrize("value", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   "])
def test_no_bearer_token_is_no_key(strata: Strata, dm: tuple[str, str], value: str) -> None:
    _, rpt = dm
    resp = strata.client.get(f"/r/{rpt}.csv", headers={**ANON, "Authorization": value})
    assert resp.status_code == 401


def test_a_header_key_never_opens_the_page(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    resp = strata.client.get(f"/r/{rpt}.html", headers=_bearer(token))
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_header_keyed_requests_are_in_the_read_audit(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid)
    strata.client.get(f"/r/{rpt}.csv", headers=_bearer(token))
    strata.client.get(f"/r/{rpt}.csv", headers=_bearer("wrongtoken"))
    with strata.app.app_context():
        from strata.db import get_db

        rows = (
            get_db()
            .execute(
                "SELECT status, api_key_id IS NOT NULL, token_prefix FROM read_log ORDER BY id"
            )
            .fetchall()
        )
    assert rows == [(200, 1, token[:8]), (403, 0, "wrongtok")]


def test_the_excel_key_cannot_be_deleted_only_replaced_or_disabled(
    strata: Strata, dm: tuple[str, str]
) -> None:
    dm_uuid, rpt = dm
    with strata.app.app_context():
        from strata.models.api_key import ApiKey

        excel = ApiKey.get_excel_link(strata.datamodule(dm_uuid).id)
        assert excel is not None
    resp = strata.client.post(f"/datamodules/apikeys/{excel.uuid}/delete")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#excel-link")
    assert ("error", "The Excel key cannot be deleted: replace it or disable it.") in (
        strata.flashes()
    )
    with strata.app.app_context():
        still = ApiKey.get_excel_link(strata.datamodule(dm_uuid).id)
        assert still is not None and still.id == excel.id
    strata.client.post(f"/datamodules/apikeys/{excel.uuid}/toggle")  # disable
    box = strata.client.get(f"/r/{rpt}/excel").get_data(as_text=True)
    assert "This Excel key is disabled" in box


def test_named_keys_start_closed_and_a_key_action_opens_them(
    strata: Strata, dm: tuple[str, str]
) -> None:
    dm_uuid, _rpt = dm
    page = _page(strata, dm_uuid)
    assert '<details id="named-keys" class="section-details">' in page  # closed
    resp = strata.client.post(f"/datamodules/{dm_uuid}/apikeys", data={"name": "finance"})
    assert resp.headers["Location"].endswith("#named-keys")  # app.js opens it


def test_a_named_keys_links_leave_out_what_it_fixes(strata: Strata, dm: tuple[str, str]) -> None:
    dm_uuid, rpt = dm
    token = strata.new_api_key(dm_uuid, name="north only", fixed_args="region=North")
    page = _page(strata, dm_uuid)
    assert f'data-copy="http://localhost/r/{rpt}.xlsx?key={token}"' in page
