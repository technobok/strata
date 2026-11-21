"""Build scripts as Jinja templates: loops over rows of a dependency.

A build script that contains ``{{`` or ``{%`` is rendered with Jinja's sandboxed
environment before it runs (after its dependencies are resolved), so one script
can repeat a statement per company:

    CREATE TABLE stock AS
    {% for c in rows('companies', "SELECT code, db FROM companies ORDER BY code") %}
    {% if not loop.first %}UNION ALL{% endif %}
    SELECT {{ c.code | sqlstr }} AS company, *
    FROM odbc_query(getvariable('conn_navi'), {{ ("SELECT * FROM " ~ c.db ~ ".dbo.Stock") | sqlstr }})
    {% endfor %}

- ``rows(local_name, sql)``: the rows (as dicts) of one SELECT on a
  dependency's version, run through the serve sandbox. At most ``MAX_ROWS``.
- ``| sqlstr``: a quoted SQL string literal (``None`` -> ``NULL``).
- ``| ident``: a quoted SQL identifier.

The rendered text is what runs and what the version keeps as its script.
Errors name the template line. A script without ``{{``/``{%`` is untouched.
"""

import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb
from jinja2 import StrictUndefined, TemplateError, TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from strata.services import duck

MAX_ROWS = 10_000


class ScriptTemplateError(RuntimeError):
    """The build script's template could not be rendered (message names the line)."""


def is_template(script: str) -> bool:
    return "{{" in script or "{%" in script


def sqlstr(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def ident(value: Any) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _template_line(exc: BaseException) -> int | None:
    if isinstance(exc, TemplateSyntaxError):
        return exc.lineno
    line = None
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        if frame.f_code.co_filename == "<template>":
            line = lineno
    return line


def render(script: str, dependencies: Mapping[str, Path | None]) -> str:
    """Render ``script`` if it is a template; ``dependencies`` maps each local
    name to the version file ``rows()`` reads (None: never built). Raises
    ScriptTemplateError."""
    if not is_template(script):
        return script

    def rows(local_name: str, sql: str) -> list[dict[str, Any]]:
        if local_name not in dependencies:
            known = ", ".join(sorted(dependencies)) or "none"
            raise ScriptTemplateError(
                f"rows('{local_name}', ...): no dependency of that name (dependencies: {known})"
            )
        duck.validate_select(sql, f"The query in rows('{local_name}', ...)")
        version_file = dependencies[local_name]
        if version_file is None:
            raise ScriptTemplateError(
                f"rows('{local_name}', ...): that dependency has never been built"
            )
        conn = duck.serve_conn(version_file)
        try:
            cursor = conn.execute(sql)
            columns = [str(d[0]) for d in cursor.description or []]
            found = cursor.fetchmany(MAX_ROWS + 1)
        finally:
            conn.close()
        if len(found) > MAX_ROWS:
            raise ScriptTemplateError(
                f"rows('{local_name}', ...) returned more than {MAX_ROWS:,} rows"
            )
        return [dict(zip(columns, row, strict=True)) for row in found]

    env = SandboxedEnvironment(undefined=StrictUndefined, keep_trailing_newline=True)
    env.filters["sqlstr"] = sqlstr
    env.filters["ident"] = ident
    try:
        return env.from_string(script).render(rows=rows)
    except (TemplateError, ScriptTemplateError, duck.SqlShapeError, duckdb.Error) as exc:
        line = _template_line(exc)
        where = f"line {line}" if line else "the template"
        message = exc.message if isinstance(exc, TemplateSyntaxError) else str(exc)
        raise ScriptTemplateError(f"build script, {where}: {message}") from exc
