"""Text that people write and Strata shows as HTML, and names made from names.

- ``markdown``: a report's description is Markdown (paragraphs, lists, **bold**,
  links, tables). It is written by people, not shipped in the repo like the help
  pages, so raw HTML is escaped, a ``javascript:``/``data:`` link is neutralised
  (mistune's ``safe_url``) and an image shows as its alt text: a report page or a
  mail never loads anything from another server.
- ``local_name``: the local name the datamodule page suggests for a dependency (the
  last part of its path) or a connection (its name).
"""

import re

import mistune
from markupsafe import Markup, escape


class _PeoplesMarkdown(mistune.HTMLRenderer):
    def image(self, text: str, url: str, title: str | None = None) -> str:
        return str(escape(text))

    def link(self, text: str, url: str, title: str | None = None) -> str:
        return super().link(text, url, title).replace("<a ", '<a rel="noopener noreferrer" ', 1)


_MARKDOWN = mistune.create_markdown(
    escape=True,
    renderer=_PeoplesMarkdown(escape=True),
    plugins=["strikethrough", "table"],
)


def markdown(text: str | None) -> Markup:
    """``text`` (Markdown written by a person) as safe HTML."""
    if not text or not text.strip():
        return Markup("")
    return Markup(str(_MARKDOWN(text)))


def local_name(text: str | None) -> str:
    """A SQL-safe local name from a datamodule's path (its last part:
    ``ref/companies`` → ``companies``) or a connection's name: letters, digits and
    ``_``, never starting with a digit."""
    last = (text or "").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9_]+", "_", last).strip("_")
    if name and name[0].isdigit():
        name = f"_{name}"
    return name
