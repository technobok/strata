"""Link templates: an address (or a link text) filled from the current row.

- ``{Column}`` is the value, percent-encoded, so spaces, ``&`` and ``/`` cannot break the
  link. Column names may contain spaces: ``{Customer Code}``.
- ``{+Column}`` is the value as it is (RFC 6570's reserved expansion), for a column that
  already holds a path or a whole address.
- ``{{`` and ``}}`` are literal braces.

Where a link goes is decided by how it starts: a scheme (``https:``) = that address;
``/`` = the same site; anything else = within Strata. Allowed schemes: http, https,
mailto, plus the setting ``links.extra_schemes``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

DEFAULT_SCHEMES = ("http", "https", "mailto")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# Browsers drop tabs and newlines anywhere in an address ("java\tscript:" runs), and
# leading spaces and control characters: an address with any is refused, not cleaned.
_UNSAFE_RE = re.compile(r"[\x00-\x20\x7f]")
_RESERVED = ":/?#[]@!$&'()*+,;=%"  # {+Col} keeps these (and an existing %XX) as they are


def allowed_schemes() -> tuple[str, ...]:
    """http, https and mailto, plus the live setting ``links.extra_schemes``."""
    from strata.config import live_list

    extra = (s.strip().lower().rstrip(":") for s in live_list("links.extra_schemes"))
    return DEFAULT_SCHEMES + tuple(s for s in extra if s and s not in DEFAULT_SCHEMES)


class TemplateError(ValueError):
    def __init__(self, message: str, position: int | None = None) -> None:
        self.message = message
        self.position = position
        where = "" if position is None else f" (at character {position + 1})"
        super().__init__(message + where)


@dataclass(frozen=True)
class Part:
    text: str  # literal text, or the column name of a placeholder
    column: bool = False
    reserved: bool = False  # {+Col}: inserted as it is


@dataclass(frozen=True)
class Template:
    source: str
    parts: tuple[Part, ...]

    def columns(self) -> list[str]:
        """The columns its placeholders name, in order, each once."""
        seen: list[str] = []
        for p in self.parts:
            if p.column and p.text not in seen:
                seen.append(p.text)
        return seen

    def scheme(self) -> str | None:
        """The scheme its fixed start gives (``"https"``), ``""`` for none (a link within
        the site or Strata), or None when a ``{+Col}`` value decides it at render."""
        if not self.parts:
            return ""
        first = self.parts[0]
        if first.column:
            return None if first.reserved else ""
        match = _SCHEME_RE.match(first.text)
        return match.group(1).lower() if match else ""


def fill(template: Template, values: Mapping[str, str | None], encode: bool) -> str | None:
    """The template with each placeholder's value; None when a value is NULL or empty
    (the cell then shows plain text). ``encode``: ``{Col}`` is percent-encoded and
    ``{+Col}`` keeps reserved characters (an address); otherwise values go in as they
    are (a link text)."""
    out: list[str] = []
    for part in template.parts:
        if not part.column:
            out.append(part.text)
            continue
        value = values.get(part.text)
        if value is None or value == "":
            return None
        if encode:
            value = quote(value, safe=_RESERVED if part.reserved else "")
        out.append(value)
    return "".join(out)


def safe_address(address: str, root: str, schemes: tuple[str, ...]) -> str | None:
    """Where a filled link goes, or None (plain text) when it may not go anywhere:
    a scheme that is not allowed, control characters or spaces, or ``//host`` (another
    site without a scheme). ``/…`` stays on the same site; anything else is within
    Strata, under ``root``."""
    if not address or _UNSAFE_RE.search(address) or "\\" in address[:2]:
        return None
    match = _SCHEME_RE.match(address)
    if match:
        return address if match.group(1).lower() in schemes else None
    if address.startswith("//"):
        return None
    if address.startswith("/"):
        return address
    return root.rstrip("/") + "/" + address


def parse(source: str) -> Template:
    parts: list[Part] = []
    literal: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "{" and source[i + 1 : i + 2] == "{":
            literal.append("{")
            i += 2
        elif ch == "}" and source[i + 1 : i + 2] == "}":
            literal.append("}")
            i += 2
        elif ch == "{":
            end = source.find("}", i + 1)
            if end < 0:
                raise TemplateError("A { has no closing }: write {{ for a brace", i)
            inner = source[i + 1 : end]
            reserved = inner.startswith("+")
            name = inner[1:] if reserved else inner
            if not name:
                raise TemplateError("{} names no column: write {Column}", i)
            if "{" in name:
                raise TemplateError("A { inside a placeholder: write {{ for a brace", i)
            if literal:
                parts.append(Part("".join(literal)))
                literal = []
            parts.append(Part(name, column=True, reserved=reserved))
            i = end + 1
        elif ch == "}":
            raise TemplateError("A } without a {: write }} for a brace", i)
        else:
            literal.append(ch)
            i += 1
    if literal:
        parts.append(Part("".join(literal)))
    return Template(source, tuple(parts))
