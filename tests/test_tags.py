"""Tags group datamodules: set in the datamodule form from the tags an admin made,
shown on the Datamodules page, which filters by one (``?tag=``)."""

from typing import Any

from tests.conftest import Strata


def _make_tags(strata: Strata, *names: str) -> None:
    for name in names:
        assert strata.client.post("/tags/new", data={"name": name}).status_code == 302


def _tags_of(strata: Strata, dm_uuid: str) -> list[str]:
    with strata.app.app_context():
        from strata.models.datamodule import Datamodule
        from strata.models.tag import Tag

        dm = Datamodule.get_by_uuid(dm_uuid)
        assert dm is not None
        return [t.name for t in Tag.for_datamodule(dm.id)]


def _save(strata: Strata, dm_uuid: str, tags: str) -> Any:
    return strata.client.post(
        f"/datamodules/{dm_uuid}/edit",
        data={"path": "test/sales", "tags_present": "1", "tags": tags},
    )


def test_the_form_sets_tags_and_the_list_filters_by_one(strata: Strata) -> None:
    _make_tags(strata, "Finance", "testrunner")
    sales = strata.new_datamodule()
    other = strata.new_datamodule(path="test/other")
    assert _save(strata, sales, "finance, TESTRUNNER, finance").status_code == 302
    assert _tags_of(strata, sales) == ["Finance", "testrunner"]
    assert _tags_of(strata, other) == []

    page = strata.client.get("/datamodules/").get_data(as_text=True)
    assert 'href="/datamodules/?tag=Finance"' in page and "test/other" in page
    only = strata.client.get("/datamodules/?tag=finance").get_data(as_text=True)
    assert "test/sales" in only and "test/other" not in only
    form = strata.client.get(f"/datamodules/{sales}/edit").get_data(as_text=True)
    assert 'name="tags" value="Finance, testrunner"' in form


def test_an_unknown_tag_is_refused_and_what_was_typed_stays(strata: Strata) -> None:
    _make_tags(strata, "finance")
    dm = strata.new_datamodule()
    resp = _save(strata, dm, "finance, nosuch")
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "there is no tag named &#39;nosuch&#39;" in page
    assert 'name="tags" value="finance, nosuch"' in page
    assert _tags_of(strata, dm) == []


def test_a_post_without_the_tags_field_keeps_them(strata: Strata) -> None:
    _make_tags(strata, "finance")
    dm = strata.new_datamodule()
    assert _save(strata, dm, "finance").status_code == 302
    strata.set_script(dm, "CREATE TABLE t AS SELECT 1 AS x")
    assert _tags_of(strata, dm) == ["finance"]
    assert _save(strata, dm, "").status_code == 302  # an emptied field clears them
    assert _tags_of(strata, dm) == []


def test_a_deleted_tag_leaves_its_datamodules(strata: Strata) -> None:
    _make_tags(strata, "finance")
    dm = strata.new_datamodule()
    assert _save(strata, dm, "finance").status_code == 302
    with strata.app.app_context():
        from strata.models.tag import Tag

        tag = Tag.get_by_name("finance")
        assert tag is not None
        tag_id = tag.id
    assert strata.client.post(f"/tags/{tag_id}/delete").status_code == 302
    assert _tags_of(strata, dm) == []
