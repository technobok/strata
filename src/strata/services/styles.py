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
import re

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
