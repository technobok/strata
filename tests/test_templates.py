"""app.js removes every .flash element 5 seconds after a page loads, so only flash
messages may use it: an error the reader must be able to read stays (.error-box)."""

import re
from pathlib import Path

from strata.blueprints import help as help_bp
from tests.conftest import Strata

TEMPLATES = Path(help_bp.__file__).parent.parent / "templates"


def test_only_flash_messages_use_the_flash_class() -> None:
    users = sorted(
        str(path.relative_to(TEMPLATES))
        for path in TEMPLATES.rglob("*.html")
        if re.search(r'class="[^"]*\bflash\b', path.read_text())
    )
    assert users == ["base.html"]


def test_a_build_error_stays_on_the_page(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(script="CREATE TABLE t AS SELECT * FROM nowhere_xyz")
    strata.build(dm_uuid)
    with strata.client.session_transaction() as sess:
        sess.pop("_flashes", None)
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert '<article class="error-box">Last build error:' in page and "nowhere_xyz" in page


# Buttons and links (S3, 2026-11-23): <a> goes somewhere, <button> does something,
# and both get their size from one place (app.css), so a row of them lines up.


def _templates_matching(pattern: str) -> list[str]:
    return sorted(
        str(path.relative_to(TEMPLATES))
        for path in TEMPLATES.rglob("*.html")
        if re.search(pattern, path.read_text())
    )


def test_navigation_is_a_link_not_a_script_button() -> None:
    """A button that runs location.href= cannot be opened in a new tab, copied or
    followed without JS: navigation is an <a role="button">."""
    assert _templates_matching(r"location\.href") == []


def test_layout_lives_in_classes_not_inline_flex() -> None:
    """Rows of controls use .actions / .inline-form / .section-header; an inline flex
    on a <td> also stops it being a table cell (its border leaves the row)."""
    assert _templates_matching(r'style="[^"]*display:\s*flex') == []


def test_confirms_are_data_confirm() -> None:
    """One way to ask first: data-confirm on the form (post_button), handled by app.js.
    An inline confirm('…{{ name }}…') breaks on a name with a quote."""
    assert _templates_matching(r"onsubmit=|return confirm\(") == []


def test_post_button_asks_with_data_confirm(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule()
    page = strata.client.get(f"/datamodules/{dm_uuid}/edit").get_data(as_text=True)
    assert (
        f'<form method="post" action="/datamodules/{dm_uuid}/delete" '
        'data-confirm="Delete this datamodule and all its versions?">'
        '<button type="submit" class="outline secondary">Delete</button></form>'
    ) in page
