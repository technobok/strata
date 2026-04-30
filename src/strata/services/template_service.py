"""Template service for SQL template parsing, parameter extraction, and rendering."""

import re

from jinja2 import Environment, TemplateSyntaxError, meta

# Pattern for DuckDB bind parameters: $name or $name123
_BIND_PARAM_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")

# Allowed chars for structural parameter values (identifiers, connection strings)
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_.]+$")
_CONNECTION_STRING_RE = re.compile(r"^[a-zA-Z0-9_.=;{}\-\\/: @,]+$")


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

    # Extract value parameters from $var patterns
    for match in _BIND_PARAM_RE.finditer(sql_template):
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
) -> str:
    """Render Jinja2 structural parameters in the SQL template.

    Only structural parameters are rendered here. DuckDB bind parameters
    ($var) are left untouched for the query engine.

    A `ref(name)` Jinja function is registered: it expands to
    `mat_<name>.result` (the convention for materialised reports) and, when
    `refs_collector` is provided, appends the referenced names so the caller
    can ATTACH the matching .duckdb files before executing.
    """
    env = Environment()

    def _ref(name: str) -> str:
        if refs_collector is not None and name not in refs_collector:
            refs_collector.append(name)
        return f"mat_{name}.result"

    env.globals["ref"] = _ref
    template = env.from_string(sql_template)
    return template.render(**structural_params)


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
