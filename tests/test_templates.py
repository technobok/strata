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
