"""In-app help: the repo's docs/*.md rendered at /help/<page>.

The pages are repo-authored (trusted), so raw HTML in them passes through.
Headings get GitHub-style anchors, so a link between pages is written
``urls.md#filters`` and works both on GitHub and here: ``/help/urls.md``
redirects to ``/help/urls`` and the browser keeps the anchor.

Templates link to a section with the ``help_link(page, anchor)`` Jinja global
(a small "?" that opens the page in a new tab, so an unsaved edit survives);
tests check every such link's page and anchor exist.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import mistune
from flask import Blueprint, abort, redirect, render_template, url_for
from markupsafe import Markup
from mistune.toc import add_toc_hook
from werkzeug.wrappers import Response

from strata.blueprints.auth import login_required
from strata.paths import repo_dir

bp = Blueprint("help", __name__, url_prefix="/help")

# Every docs/*.md page, in sidebar order (a test checks the two agree).
PAGES: dict[str, str] = {
    "index": "How Strata fits together",
    "build-scripts": "Build scripts",
    "reports": "Reports",
    "checks": "Checks and runs",
    "urls": "Report URLs",
    "api-keys": "API keys and Excel",
    "freshness": "Freshness and builds",
    "schedules": "Schedules",
    "access": "Access",
    "audit": "Read audit",
    "import-export": "Import and export",
    "settings": "Settings (admin)",
}


@dataclass(frozen=True)
class HelpPage:
    name: str
    title: str
    html: Markup
    toc: list[tuple[str, str]]  # (anchor, text) of each level-2 heading
    anchors: frozenset[str]  # every heading id on the page


def slugify(text: str, seen: dict[str, int]) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to '-';
    a repeated heading gets '-1', '-2', ..."""
    base = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}-{n}"


def docs_dir() -> Path:
    return repo_dir("docs")


def render(name: str) -> HelpPage:
    path = docs_dir() / f"{name}.md"
    return _render(name, str(path), path.stat().st_mtime_ns)


@lru_cache(maxsize=32)
def _render(name: str, path: str, _mtime_ns: int) -> HelpPage:
    seen: dict[str, int] = {}

    def heading_id(token: dict[str, Any], _index: int) -> str:
        return slugify(str(token["text"]), seen)

    md = mistune.create_markdown(escape=False, plugins=["table", "strikethrough"])
    add_toc_hook(md, min_level=1, max_level=6, heading_id=heading_id)
    html, state = md.parse(Path(path).read_text())
    items = state.env.get("toc_items", [])
    return HelpPage(
        name=name,
        title=PAGES[name],
        html=Markup(str(html)),
        toc=[(anchor, text) for level, anchor, text in items if level == 2],
        anchors=frozenset(anchor for _level, anchor, _text in items),
    )


def help_link(page: str, anchor: str = "") -> Markup:
    """The contextual "?" next to an editor or section heading."""
    href = url_for("help.page", name=page, _anchor=anchor or None)
    return Markup(
        '<a class="help-link" href="{}" target="_blank" rel="noopener" '
        'title="Help" aria-label="Help">?</a>'
    ).format(href)


@bp.route("/")
@login_required
def index() -> str | Response:
    return page("index")


@bp.route("/<name>")
@login_required
def page(name: str) -> str | Response:
    if name.endswith(".md"):  # a link written for GitHub
        return redirect(url_for("help.page", name=name.removesuffix(".md")))
    if name not in PAGES:
        abort(404)
    return render_template("help/page.html", doc=render(name), pages=PAGES)
