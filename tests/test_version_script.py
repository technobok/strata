"""Each version keeps the build script it was built from, shown from its row of
the datamodule's Versions table."""

import html

from tests.conftest import SALES_SCRIPT, Strata

OTHER_SCRIPT = "CREATE TABLE totals AS SELECT 'North' AS region, 175.25 AS revenue"


def _page(strata: Strata, dm_uuid: str, version_uuid: str, **headers: str) -> tuple[int, str]:
    resp = strata.client.get(
        f"/datamodules/{dm_uuid}/versions/{version_uuid}/script", headers=headers
    )
    return resp.status_code, html.unescape(resp.get_data(as_text=True))


def test_each_version_shows_the_script_it_was_built_from(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    strata.set_script(dm_uuid, OTHER_SCRIPT)
    strata.build(dm_uuid)
    new, old = strata.versions(dm_uuid)  # newest first
    assert (new.status, old.status) == ("ready", "superseded")

    status, page = _page(strata, dm_uuid, old.uuid)
    assert status == 200
    assert SALES_SCRIPT.strip() in page and OTHER_SCRIPT not in page
    assert "The current script has changed since." in page
    status, page = _page(strata, dm_uuid, new.uuid)
    assert OTHER_SCRIPT in page and "The current script is the same." in page and "live" in page

    edit = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    for v in (new, old):
        assert f'href="/datamodules/{dm_uuid}/versions/{v.uuid}/script"' in edit


def test_a_failed_version_keeps_its_script(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(script="CREATE TABLE t AS SELECT * FROM nowhere_xyz")
    strata.build(dm_uuid)
    [failed] = strata.versions(dm_uuid)
    assert failed.status == "failed"
    assert "FROM nowhere_xyz" in _page(strata, dm_uuid, failed.uuid)[1]


def test_a_jinja_version_shows_the_text_that_ran(strata: Strata) -> None:
    script = (
        "CREATE TABLE t AS {% for i in range(2) %}{% if not loop.first %}UNION ALL "
        "{% endif %}SELECT {{ i }} AS n {% endfor %}"
    )
    dm_uuid = strata.new_datamodule(script=script)
    strata.build(dm_uuid)
    [version] = strata.versions(dm_uuid)
    page = _page(strata, dm_uuid, version.uuid)[1]
    assert "SELECT 0 AS n UNION ALL SELECT 1 AS n" in page and "{%" not in page
    assert "has changed since" not in page and "is the same" not in page


def test_only_this_datamodules_versions_and_only_for_owners(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    other = strata.new_datamodule(path="test/other")
    [version] = strata.versions(dm_uuid)
    assert _page(strata, other, version.uuid)[0] == 404
    assert _page(strata, dm_uuid, "0" * 32)[0] == 404
    assert _page(strata, dm_uuid, version.uuid, **{"X-User": "viewer"})[0] == 403
