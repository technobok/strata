"""The named styles a report's style rules may apply (user decision 2026-11-25: named
styles only, no raw CSS).

A style is space-separated words:

- ``bold`` ``italic`` ``underline`` ``strike``;
- a Pico colour family for the text colour (``red`` … ``slate``): theme-aware, Strata
  picks a shade that reads in light and in dark mode;
- ``bg-<family>`` for a background, theme-aware too;
- an exact Pico shade, ``red-550`` / ``bg-red-100`` (shades 50 … 950): the same in both
  themes.

At most one text colour and one background per style.
"""

import difflib
import functools
import re
from collections.abc import Sequence

from strata.services.pico_palette import PALETTE

WORDS = ("bold", "italic", "underline", "strike")
FAMILIES = (
    "red",
    "pink",
    "fuchsia",
    "purple",
    "violet",
    "indigo",
    "blue",
    "azure",
    "cyan",
    "jade",
    "green",
    "lime",
    "yellow",
    "amber",
    "pumpkin",
    "orange",
    "sand",
    "grey",
    "zinc",
    "slate",
)
SHADES = tuple(range(50, 1000, 50))  # Pico's 50, 100, 150 … 950

_SHADE_RE = re.compile(r"^(bg-)?([a-z]+)-(\d+)$")
_VOCABULARY = WORDS + FAMILIES + tuple(f"bg-{f}" for f in FAMILIES)


class StyleError(ValueError):
    pass


def parse(text: str) -> list[str]:
    """The style's words, lowercased, each once; StyleError says what is wrong."""
    words: list[str] = []
    colour = background = None
    for word in text.lower().split():
        if word in words:
            continue
        _check(word)
        if word not in WORDS:
            if word.startswith("bg-"):
                if background is not None:
                    raise StyleError(f"Two backgrounds: {background} and {word}")
                background = word
            else:
                if colour is not None:
                    raise StyleError(f"Two text colours: {colour} and {word}")
                colour = word
        words.append(word)
    if not words:
        raise StyleError("A style needs at least one word, e.g. red bold")
    return words


def _check(word: str) -> None:
    if word in _VOCABULARY:
        return
    shade = _SHADE_RE.match(word)
    if shade and shade.group(2) in FAMILIES:
        if int(shade.group(3)) not in SHADES:
            raise StyleError(f"'{word}': Pico's shades are 50, 100, 150 … 950 (steps of 50)")
        return
    near = difflib.get_close_matches(word, _VOCABULARY, n=1, cutoff=0.6)
    hint = f" (did you mean '{near[0]}'?)" if near else ""
    raise StyleError(
        f"'{word}' is not a style{hint}: use bold, italic, underline, strike, a colour such "
        "as red, a background such as bg-red, or a shade such as red-550"
    )


# ---------------------------------------------------------------------------
# Showing styles
# ---------------------------------------------------------------------------

# A family name is theme-aware: these shades of it, per theme. tests/test_styles.py
# checks every family keeps WCAG contrast >= 4.5 against Pico's own background and text
# (light: white / #373c44; dark: #13161e / #c2c7d0), and its text on its background.
TEXT_SHADE = {"light": 600, "dark": 400}
BACKGROUND_SHADE = {"light": 100, "dark": 850}
# Excel's [Black] / [White] (a format's colour): the theme's text and background.
THEME_COLOURS = ("black", "white")
# Mail is drawn as Pico's light theme.
MAIL_TEXT, MAIL_BACKGROUND = "#373c44", "#ffffff"


def _kind(word: str) -> str:
    """deco, colour (text) or background."""
    if word in WORDS:
        return "deco"
    return "background" if word.startswith("bg-") else "colour"


def merge(styles: Sequence[Sequence[str]], format_colour: str | None = None) -> tuple[str, ...]:
    """One cell's words from the style rules that apply to it, in order: a later rule's
    colour or background replaces an earlier one's, words add up. A format's colour
    (``[Red]``) shows only when no rule sets a text colour."""
    words: list[str] = []
    colour: str | None = None
    background: str | None = None
    for style in styles:
        for word in style:
            kind = _kind(word)
            if kind == "colour":
                colour = word
            elif kind == "background":
                background = word
            elif word not in words:
                words.append(word)
    colour = colour or format_colour
    return tuple(words + [w for w in (colour, background) if w])


def _shade_var(word: str) -> str:
    name = word.removeprefix("bg-")
    return f"var(--pico-color-{name})"


@functools.cache
def page_attrs(words: tuple[str, ...]) -> tuple[str, str]:
    """(class list, inline style) for a cell on the page: classes for the words and
    the theme-aware families (static/css/styles.css), an inline colour only for an
    exact shade (a validated word, so nothing else can get into the attribute)."""
    classes: list[str] = []
    inline: list[str] = []
    for word in words:
        if word in WORDS or word in THEME_COLOURS:
            classes.append(f"st-{word}")
        elif word.removeprefix("bg-") in FAMILIES:
            classes.append(f"st-{word}")
        elif _SHADE_RE.match(word):
            prop = "background-color" if word.startswith("bg-") else "color"
            inline.append(f"{prop}:{_shade_var(word)}")
    return " ".join(classes), ";".join(inline)


@functools.cache
def mail_css(words: tuple[str, ...]) -> str:
    """Inline CSS for a cell in mail (hex colours: mail clients know no variables)."""
    css: list[str] = []
    lines: list[str] = []
    for word in words:
        if word == "bold":
            css.append("font-weight:700")
        elif word == "italic":
            css.append("font-style:italic")
        elif word in ("underline", "strike"):
            lines.append("underline" if word == "underline" else "line-through")
        elif word == "black":
            css.append(f"color:{MAIL_TEXT}")
        elif word == "white":
            css.append(f"color:{MAIL_BACKGROUND}")
        else:
            prop = "background-color" if word.startswith("bg-") else "color"
            name = word.removeprefix("bg-")
            if name in FAMILIES:
                shade = BACKGROUND_SHADE if prop == "background-color" else TEXT_SHADE
                name = f"{name}-{shade['light']}"
            if name in PALETTE:
                css.append(f"{prop}:{PALETTE[name]}")
    if lines:
        css.append(f"text-decoration:{' '.join(lines)}")
    return "".join(f"{c};" for c in css)


def palette_css() -> str:
    """static/css/styles.css: the classes page_attrs uses (tests/test_styles.py checks
    the file is this text)."""
    out = [
        "/* Report styles (services/styles.py). GENERATED by styles.palette_css():",
        "   tests/test_styles.py fails when this file and the generator differ. */",
        ".st-bold{font-weight:700}",
        ".st-italic{font-style:italic}",
        ".st-underline{text-decoration-line:underline}",
        ".st-strike{text-decoration-line:line-through}",
        ".st-underline.st-strike{text-decoration-line:underline line-through}",
        ".st-black{color:var(--pico-color)}",
        ".st-white{color:var(--pico-background-color)}",
    ]
    dark: list[str] = []
    for family in FAMILIES:
        for prefix, prop, shades in (
            ("", "color", TEXT_SHADE),
            ("bg-", "background-color", BACKGROUND_SHADE),
        ):
            cls = f".st-{prefix}{family}"
            out.append(f"{cls}{{{prop}:var(--pico-color-{family}-{shades['light']})}}")
            rule = f"{prop}:var(--pico-color-{family}-{shades['dark']})"
            out.append(f"[data-theme=dark] {cls}{{{rule}}}")
            dark.append(f":root:not([data-theme]) {cls}{{{rule}}}")
    out.append("@media (prefers-color-scheme:dark){")
    out += [f"  {line}" for line in dark]
    out.append("}")
    return "\n".join(out) + "\n"


def palette_grid_html() -> str:
    """The help page's palette: each family's theme-aware text and background on a light
    and a dark panel, then every exact shade (name on hover) on both."""

    def families(theme: str) -> str:
        items = []
        for family in FAMILIES:
            text = f"var(--pico-color-{family}-{TEXT_SHADE[theme]})"
            back = f"var(--pico-color-{family}-{BACKGROUND_SHADE[theme]})"
            items.append(
                f'<span><span style="color:{text}">{family}</span><br>'
                f'<span class="palette-bg" style="background-color:{back}">bg-{family}</span></span>'
            )
        return "".join(items)

    def shades() -> str:
        head = "".join(f"<th>{s}</th>" for s in SHADES)
        rows = []
        for family in FAMILIES:
            cells = "".join(
                f'<td title="{family}-{s}" style="background-color:var(--pico-color-{family}-{s})"></td>'
                for s in SHADES
            )
            rows.append(f"<tr><th>{family}</th>{cells}</tr>")
        return f'<table class="palette-shades"><tr><th></th>{head}</tr>{"".join(rows)}</table>'

    panels = []
    for theme in ("light", "dark"):
        panels.append(
            f'<div class="palette-panel" data-theme="{theme}"><p><strong>{theme.title()} '
            f'theme</strong>: the families</p><div class="palette-families">{families(theme)}</div>'
            f"<p>Every shade (e.g. <code>red-550</code>, <code>bg-red-100</code>):</p>"
            f'<div class="table-scroll">{shades()}</div></div>'
        )
    return '<div class="palette">' + "".join(panels) + "</div>"
