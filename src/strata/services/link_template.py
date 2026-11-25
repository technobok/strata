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
from dataclasses import dataclass

DEFAULT_SCHEMES = ("http", "https", "mailto")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")


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
