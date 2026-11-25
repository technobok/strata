"""Style rules and named styles (services/styles.py): Pico's palette vendored and in
step with Strata's tables, theme-aware colours that read in light and dark mode (WCAG),
rules evaluated by DuckDB with the page's rows, and the Format page's Styles section."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from strata.services import styles
from strata.services.pico_palette import PALETTE
from tests.conftest import Strata, as_user
from tests.test_format_page import _form, _url
from tests.test_formatting_render import _setup
from tests.test_notify import _fire, _schedule
from tests.test_presentation import _import, _report

STATIC = Path(__file__).resolve().parents[1] / "src" / "strata" / "static"


def _vendored_shades() -> dict[str, str]:
    css = (STATIC / "vendor" / "pico.colors.min.css").read_text()
    return dict(re.findall(r"--pico-color-([a-z]+-\d+):(#[0-9a-f]{6})", css))


def test_the_vendored_palette_has_every_family_and_shade_and_the_table_matches() -> None:
    shades = _vendored_shades()
    assert set(shades) == {f"{f}-{s}" for f in styles.FAMILIES for s in styles.SHADES}
    assert PALETTE == shades  # services/pico_palette.py is generated from it


def test_the_style_sheet_is_what_the_generator_writes() -> None:
    assert (STATIC / "css" / "styles.css").read_text() == styles.palette_css()


def _theme_colours() -> dict[str, tuple[str, str]]:
    """Pico's own (background, text) per theme, from the vendored pico.min.css."""
    css = (STATIC / "vendor" / "pico.min.css").read_text()
    out = {}
    for theme, selector in (("light", r"\[data-theme=light\]"), ("dark", r"\[data-theme=dark\]")):
        block = re.search(selector + r"\{([^}]*)\}", css)
        assert block is not None, theme
        bg = re.search(r"--pico-background-color:([^;]+)", block.group(1))
        text = re.search(r"--pico-color:([^;]+)", block.group(1))
        assert bg and text
        out[theme] = (_hex(bg.group(1)), _hex(text.group(1)))
    return out


def _hex(value: str) -> str:
    value = value.strip()
    if value.startswith("#"):
        return value if len(value) == 7 else "#" + "".join(c * 2 for c in value[1:])
    nums = [float(n) for n in re.findall(r"[\d.]+", value)[:3]]
    return "#" + "".join(f"{round(n):02x}" for n in nums)


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        def ch(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        r, g, bl = (int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(bl)

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("family", styles.FAMILIES)
def test_every_family_reads_in_both_themes(family: str, theme: str) -> None:
    """The guard for dark mode: a family's text shade on the page, the theme's text on
    its background shade, and its text on its own background all reach 4.5:1."""
    background, text = _theme_colours()[theme]
    fg = PALETTE[f"{family}-{styles.TEXT_SHADE[theme]}"]
    bg = PALETTE[f"{family}-{styles.BACKGROUND_SHADE[theme]}"]
    assert _contrast(fg, background) >= 4.5
    assert _contrast(text, bg) >= 4.5
    assert _contrast(fg, bg) >= 4.5


def test_merge_words_add_up_and_a_later_colour_wins() -> None:
    assert styles.merge([["red", "bold"], ["blue", "bg-slate", "italic"]]) == (
        "bold",
        "italic",
        "blue",
        "bg-slate",
    )
    assert styles.merge([["bold"]], "red") == ("bold", "red")  # a format's colour
    assert styles.merge([["green"]], "red") == ("green",)  # a rule's colour wins
    assert styles.merge([], None) == ()


def test_page_and_mail_attributes() -> None:
    assert styles.page_attrs(("bold", "red", "bg-amber")) == ("st-bold st-red st-bg-amber", "")
    assert styles.page_attrs(("red-550", "bg-red-100")) == (
        "",
        "color:var(--pico-color-red-550);background-color:var(--pico-color-red-100)",
    )
    assert styles.mail_css(("bold", "red", "bg-amber", "underline", "strike")) == (
        f"font-weight:700;color:{PALETTE['red-600']};background-color:{PALETTE['amber-100']};"
        "text-decoration:underline line-through;"
    )
    assert styles.mail_css(("red-550", "white")) == f"color:{PALETTE['red-550']};color:#ffffff;"


LOOK = """
path = "test/lines"

[[report]]
name = "Lines"
body = "SELECT * FROM lines"

[[report.column]]
name = "Amount"
format = "#,##0.00;[Red]-#,##0.00"

[[report.style]]
when = "\\"Company\\" = 'NZ'"
columns = []
style = "italic bg-amber"

[[report.style]]
when = '"Amount" < 0'
columns = ["Amount"]
style = "bold"

[[report.style]]
when = "\\"Company\\" = 'AU'"
columns = ["Item", "Amount"]
style = "red-550"
"""


def _styled(strata: Strata) -> str:
    rpt = _setup(strata)
    out = _import(strata, LOOK)
    assert out.refused is None, out.refused
    return rpt


def _cells(page: str, text: str) -> list[str]:
    """The opening <td ...> of each cell showing ``text``."""
    return re.findall(r"(<td[^>]*>)(?:<a [^>]*>)?" + re.escape(text) + "<", page)


def test_rules_style_the_rows_they_hold_for(strata: Strata) -> None:
    rpt = _styled(strata)
    page = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    # NZ's row: the whole row italic on amber; its Amount also bold, and red from [Red]
    assert _cells(page, "NZ") == ['<td class="st-italic st-bg-amber">']
    assert _cells(page, "-5.00") == [
        '<td class="align-right st-italic st-bold st-red st-bg-amber">'
    ]
    # AU's row: Item and Amount only, an exact shade (inline) ...
    assert _cells(page, "A 1/2") == ['<td style="color:var(--pico-color-red-550)">']
    assert _cells(page, "1,234.50") == [
        '<td class="align-right" style="color:var(--pico-color-red-550)">'
    ]
    assert _cells(page, "AU") == ["<td>"]  # ... not its other columns


def test_mail_carries_the_styles_inline(strata: Strata, mail: list[Any]) -> None:
    rpt = _styled(strata)
    assert _schedule(strata, rpt, "jsmith").status_code == 302
    _fire(strata)
    [sent] = mail
    italic_amber = f"font-style:italic;background-color:{PALETTE['amber-100']};"
    assert re.search(r'<td style="[^"]*' + re.escape(italic_amber) + '">NZ</td>', sent.html)
    assert f'color:{PALETTE["red-550"]};">A 1/2</td>' in sent.html


def test_a_rule_that_no_longer_runs_is_skipped_and_owners_are_told(strata: Strata) -> None:
    rpt = _styled(strata)
    resp = strata.client.post(
        f"/datamodules/reports/{rpt}/edit",
        data={"name": "Lines", "body_sql": 'SELECT "Item", "Amount" FROM lines'},
    )
    assert resp.status_code == 302
    page = strata.client.get(f"/r/{rpt}.html").get_data(as_text=True)
    assert (
        "Some style rules were skipped on this page: they no longer run on the data "
        "(style rule 1: Referenced column &#34;Company&#34; not found in FROM clause!; "
        "style rule 3: Referenced column &#34;Company&#34; not found in FROM clause!)" in page
    )
    assert "st-italic" not in page  # rule 1 skipped ...
    assert '<td class="align-right st-bold st-red">-5.00</td>' in page  # ... rule 2 kept
    viewer = strata.client.get(f"/r/{rpt}.html", headers=as_user("viewer"))
    assert viewer.status_code == 200
    assert "style rules were skipped" not in viewer.get_data(as_text=True)


def _rule_form(**rules: tuple[str, str, list[str]]) -> dict[str, Any]:
    data: dict[str, Any] = _form() | {"styles_present": "1"}
    for rid, (when, style, columns) in rules.items():
        data[f"r-{rid}-when"] = when
        data[f"r-{rid}-style"] = style
        data[f"r-{rid}-columns"] = columns
    return data


def test_the_format_page_saves_rules_in_page_order(strata: Strata) -> None:
    rpt = _setup(strata)
    form = _rule_form(
        n7=('"Amount" < 0', "red bold", ["Amount"]),
        n2=("\"Company\" = 'NZ'", "bg-amber", []),
        n9=("", "", []),  # an empty row added and left: nothing
    )
    assert strata.client.post(_url(rpt), data=form).status_code == 302
    from strata.services.presentation import from_json

    stored = from_json(_report(strata, "Lines", "test/lines").presentation)
    assert [(r.when, r.style, r.columns) for r in stored.styles] == [
        ('"Amount" < 0', "red bold", ["Amount"]),
        ("\"Company\" = 'NZ'", "bg-amber", []),
    ]
    page = strata.client.get(_url(rpt)).get_data(as_text=True)
    assert 'name="r-0-when" value="&#34;Amount&#34; &lt; 0"' in page
    assert 'data-field="style:2:style"' in page


def test_the_format_page_marks_a_bad_rule(strata: Strata) -> None:
    rpt = _setup(strata)
    form = _rule_form(a=('"Amonut" < 0', "red", []), b=('"Amount" < 0', "rde", []))
    data = json.loads(strata.client.post(_url(rpt, "/preview"), data=form).get_data(as_text=True))
    assert data["problems"]["style:2:style"].startswith(
        "Style 2: 'rde' is not a style (did you mean 'red'?)"
    )
    assert 'Referenced column "Amonut" not found' in data["problems"]["style:1:when"]
    refused = strata.client.post(_url(rpt), data=form)
    assert refused.status_code == 200
    assert "Not saved: the report&#39;s formatting has problems." in refused.get_data(as_text=True)


def test_a_post_without_the_styles_section_keeps_the_rules(strata: Strata) -> None:
    rpt = _styled(strata)
    assert strata.client.post(_url(rpt), data=_form()).status_code == 302
    from strata.services.presentation import from_json

    assert len(from_json(_report(strata, "Lines", "test/lines").presentation).styles) == 3


def test_the_help_page_shows_the_palette(strata: Strata) -> None:
    page = strata.client.get("/help/formatting").get_data(as_text=True)
    assert page.count('class="palette-panel"') == 2
    assert 'title="red-550" style="background-color:var(--pico-color-red-550)"' in page
    assert page.count("<td title=") == 2 * len(styles.FAMILIES) * len(styles.SHADES)
