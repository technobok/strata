"""SQL helpers shared by report serving: bind-parameter scanning, literal
stripping, identifier checks and value casting."""

import re

# Pattern for DuckDB bind parameters: $name or $name123
_BIND_PARAM_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")


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


# Connection / alias names must be SQL-identifier-safe so they can be used
# unquoted in ATTACH ... AS <alias> and in the report SQL.
ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        case "time":
            # Validate time format (HH:MM[:SS])
            from datetime import time

            return time.fromisoformat(value).isoformat()
        case "boolean":
            return value.lower() in ("true", "1", "yes", "on")
        case _:
            return value
