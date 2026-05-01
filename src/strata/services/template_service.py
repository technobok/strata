"""Template service for SQL template parsing, parameter extraction, and rendering."""

import re

from jinja2 import Environment, TemplateSyntaxError, meta

# Pattern for DuckDB bind parameters: $name or $name123
_BIND_PARAM_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")

# Allowed chars for structural parameter values (identifiers, connection strings).
# `$` is permitted because SQL Server / Sybase / Oracle allow it inside identifiers
# (e.g. dbo.events$archive); DuckDB also accepts it in quoted and unquoted identifiers.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_.$]+$")
_CONNECTION_STRING_RE = re.compile(r"^[a-zA-Z0-9_.=;{}\-\\/: @,]+$")


def _strip_literals(sql: str) -> str:
    """Replace string literals, quoted identifiers, and comments with spaces.

    Used so the bind-param regex doesn't match `$name` patterns that appear
    inside `'...'` strings, `"..."` identifiers, or `-- ...` / `/* ... */`
    comments. Length is preserved so source positions still line up if a
    caller cares.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'" or c == '"':
            quote = c
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        # SQL-style doubled-quote escape
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def extract_parameters(sql_template: str) -> list[dict[str, str]]:
    """Extract all parameters from a SQL template.

    Returns a list of dicts with 'name' and 'param_type' keys.
    Structural parameters come from Jinja2 {{ var }} syntax.
    Value parameters come from $var DuckDB bind syntax.
    """
    params: list[dict[str, str]] = []
    seen: set[str] = set()

    # Extract structural parameters from Jinja2 AST
    env = Environment()
    try:
        ast = env.parse(sql_template)
        jinja_vars = meta.find_undeclared_variables(ast)
        for name in sorted(jinja_vars):
            if name not in seen:
                params.append({"name": name, "param_type": "structural"})
                seen.add(name)
    except TemplateSyntaxError:
        pass

    # Extract value parameters from $var patterns. Skip occurrences inside
    # string/identifier literals and comments so a table name like
    # "dbo.events$archive" doesn't get treated as a bind parameter.
    scrubbed = _strip_literals(sql_template)
    for match in _BIND_PARAM_RE.finditer(scrubbed):
        name = match.group(1)
        if name not in seen:
            params.append({"name": name, "param_type": "value"})
            seen.add(name)

    return params


def validate_structural_value(name: str, value: str) -> str | None:
    """Validate a structural parameter value.

    Returns None if valid, or an error message if invalid.
    """
    if not value:
        return f"Structural parameter '{name}' cannot be empty"

    # Allow connection strings (contain special chars like =, ;, {})
    if any(c in value for c in "=;{}"):
        if not _CONNECTION_STRING_RE.match(value):
            return (
                f"Structural parameter '{name}' contains invalid characters for a connection string"
            )
        return None

    # Otherwise must be a simple identifier
    if not _IDENTIFIER_RE.match(value):
        return f"Structural parameter '{name}' must contain only letters, digits, dots, and underscores"
    return None


def render_structural(
    sql_template: str,
    structural_params: dict[str, str],
    refs_collector: list[str] | None = None,
    conns_collector: list[str] | None = None,
) -> str:
    """Render Jinja2 structural parameters in the SQL template.

    Only structural parameters are rendered here. DuckDB bind parameters
    ($var) are left untouched for the query engine.

    Two side-effect functions are registered as Jinja globals:

    - `ref(name)` — expands to `mat_<name>.result` (the convention for
      materialised reports). When `refs_collector` is provided, the name is
      appended so the caller can ATTACH the matching .duckdb cache files.

    - `conn(name)` — declares that this report uses the named connection.
      Returns the connection's name as the SQL alias, so the natural form is
      either `{% do conn('warehouse') %}` (declare-only, alias used directly
      in SQL) or `{% set wh = conn('warehouse') %}` (capture the alias
      under a shorter Jinja variable). When `conns_collector` is provided,
      the name is appended so the caller can ATTACH the connection.

    `jinja2.ext.do` is enabled so `{% do conn('name') %}` works.
    """
    env = Environment(extensions=["jinja2.ext.do"])

    def _ref(name: str) -> str:
        if refs_collector is not None and name not in refs_collector:
            refs_collector.append(name)
        return f"mat_{name}.result"

    def _conn(name: str) -> str:
        if not ALIAS_RE.match(name):
            raise ValueError(
                f"conn('{name}'): connection names must match {ALIAS_RE.pattern} "
                "(start with a letter or _, then letters/digits/underscores)"
            )
        if conns_collector is not None and name not in conns_collector:
            conns_collector.append(name)
        return name

    env.globals["ref"] = _ref
    env.globals["conn"] = _conn
    template = env.from_string(sql_template)
    return template.render(**structural_params)


# Connection / alias names must be SQL-identifier-safe so they can be used
# unquoted in ATTACH ... AS <alias> and in the report SQL.
ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def find_refs(sql_template: str) -> list[str]:
    """Statically discover ref('name') calls in a SQL template (best-effort)."""
    pattern = re.compile(r"ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
    seen: list[str] = []
    for match in pattern.finditer(sql_template):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def cast_value(value: str, data_type: str) -> object:
    """Cast a string value to the appropriate Python type for DuckDB binding.

    Raises ValueError on type mismatch.
    """
    if not value and data_type != "string":
        raise ValueError(f"Empty value cannot be cast to {data_type}")

    match data_type:
        case "string":
            return value
        case "integer":
            return int(value)
        case "float":
            return float(value)
        case "decimal":
            from decimal import Decimal

            return Decimal(value)
        case "date":
            # Validate date format
            from datetime import date

            return date.fromisoformat(value).isoformat()
        case "boolean":
            return value.lower() in ("true", "1", "yes", "on")
        case _:
            return value
