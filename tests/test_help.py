"""In-app help (/help): every page renders, and the docs cannot drift from the code.

The drift guards fail when a link in a template or a page points at a missing
page or heading, or when the code gains a name the docs do not mention: a
filter operator, a reserved URL key, a schedule kind, an argument type or
widget, or a setting.
"""

import re
from pathlib import Path

import pytest

from strata.blueprints import help as help_bp
from strata.config import REGISTRY
from strata.models.report_arg import DATA_TYPES, WIDGETS
from strata.services import query_filter, schedule_service
from tests.conftest import Strata

TEMPLATES = Path(help_bp.__file__).parent.parent / "templates"
DOCS = help_bp.docs_dir()
HELP_LINK = re.compile(r"help_link\(\s*'([^']*)'\s*(?:,\s*'([^']*)'\s*)?\)")
MD_LINK = re.compile(r"\]\(([^)\s]*)\)")


def _doc(name: str) -> str:
    return (DOCS / f"{name}.md").read_text()


def _target_exists(page: str, anchor: str) -> bool:
    return page in help_bp.PAGES and (not anchor or anchor in help_bp.render(page).anchors)


def test_every_docs_page_is_listed_and_renders(strata: Strata) -> None:
    assert sorted(p.stem for p in DOCS.glob("*.md")) == sorted(help_bp.PAGES)
    for name, title in help_bp.PAGES.items():
        resp = strata.client.get(f"/help/{name}")
        assert resp.status_code == 200, name
        page = resp.get_data(as_text=True)
        assert "<h1 id=" in page and title in page
    assert strata.client.get("/help/").status_code == 200


def test_a_github_style_link_redirects_and_unknown_pages_404(strata: Strata) -> None:
    resp = strata.client.get("/help/urls.md")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/help/urls")
    assert strata.client.get("/help/nope").status_code == 404
    assert strata.client.get("/help/../config").status_code == 404


def test_help_needs_a_login(strata: Strata) -> None:
    resp = strata.client.get("/help/urls", headers={"X-Anon": "1"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_every_help_link_in_a_template_has_its_page_and_heading() -> None:
    found = 0
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text()
        calls = HELP_LINK.findall(text)
        # Every call must be a plain literal the regex can check.
        assert len(calls) == text.count("help_link("), path
        for page, anchor in calls:
            assert _target_exists(page, anchor), f"{path.name}: help_link('{page}', '{anchor}')"
        found += len(calls)
    assert found >= 20


def test_every_link_between_docs_pages_resolves() -> None:
    for name in help_bp.PAGES:
        for target in MD_LINK.findall(_doc(name)):
            if target.startswith(("http://", "https://", "/")) or not (
                target.startswith("#") or ".md" in target
            ):
                continue
            file, _, anchor = target.partition("#")
            page = file.removesuffix(".md") if file else name
            assert _target_exists(page, anchor), f"{name}.md links to {target}"


def test_a_section_link_renders_on_the_editor(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert 'class="help-link" href="/help/build-scripts#source-files"' in page
    assert 'target="_blank"' in page


def test_every_filter_operator_and_reserved_key_is_documented() -> None:
    urls = _doc("urls")
    assert [op for op in sorted(query_filter._OPS) if f"__{op}=" not in urls] == []
    assert [k for k in sorted(query_filter.RESERVED_KEYS) if f"| `{k}` |" not in urls] == []


def test_every_schedule_kind_is_documented() -> None:
    schedules = _doc("schedules")
    assert [label for label in schedule_service.KINDS.values() if label not in schedules] == []


def test_every_argument_type_and_widget_is_documented() -> None:
    reports = _doc("reports")
    assert [v for v in (*DATA_TYPES, *WIDGETS) if f"`{v}`" not in reports] == []


def test_every_setting_is_documented() -> None:
    settings = _doc("settings")
    assert [e.key for e in REGISTRY if f"`{e.key}`" not in settings] == []


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("Source files", "source-files"),
        ("The `source()` function", "the-source-function"),
        ("Builds, the worker & you", "builds-the-worker--you"),
    ],
)
def test_anchors_follow_github(heading: str, anchor: str) -> None:
    assert help_bp.slugify(heading, {}) == anchor
