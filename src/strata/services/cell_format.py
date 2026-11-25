"""What a result cell shows when its report has no format for it, and where its column
aligns by default.

Plain means no hidden precision: integers and decimals show exactly as stored (no
rounding, no thousands separators, a DECIMAL's trailing zeros kept), a float shows its
shortest round-trip form, booleans are ``true``/``false``, and dates, times and
timestamps are ISO with a space between date and time (fractional seconds when there
are any, the offset for a value with a time zone). Numbers, dates and times align
right; text and everything else left.
"""

import re
import struct
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

LEFT, CENTER, RIGHT = "left", "center", "right"
ALIGNMENTS = (LEFT, CENTER, RIGHT)

# A DuckDB type name (as DESCRIBE spells it) → its kind. Anything not listed
# (VARCHAR, UUID, BLOB, lists, structs, enums …) is "text" or "other".
_INTEGER_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
        "BIGNUM",
    }
)
_FLOAT_TYPES = frozenset({"FLOAT", "DOUBLE"})
_TIMESTAMP_TYPES = frozenset(
    {
        "TIMESTAMP",
        "TIMESTAMP_S",
        "TIMESTAMP_MS",
        "TIMESTAMP_NS",
        "TIMESTAMP WITH TIME ZONE",
    }
)
_TIME_TYPES = frozenset({"TIME", "TIME_NS", "TIME WITH TIME ZONE"})
_TEXT_TYPES = frozenset({"VARCHAR"})

INTEGER, DECIMAL, FLOAT = "integer", "decimal", "float"
DATE, TIMESTAMP, TIME = "date", "timestamp", "time"
BOOLEAN, INTERVAL, TEXT, OTHER = "boolean", "interval", "text", "other"
NUMBER_KINDS = frozenset({INTEGER, DECIMAL, FLOAT})
DATE_TIME_KINDS = frozenset({DATE, TIMESTAMP, TIME})

_RIGHT_KINDS = NUMBER_KINDS | DATE_TIME_KINDS | {INTERVAL}
_DECIMAL_RE = re.compile(r"^DECIMAL\(\d+,\s*\d+\)$")


def kind(duck_type: str) -> str:
    """The kind of a DuckDB column type: integer, decimal, float, date, timestamp,
    time, boolean, interval, text or other."""
    name = duck_type.strip().upper()
    if name in _INTEGER_TYPES:
        return INTEGER
    if _DECIMAL_RE.match(name) or name == "DECIMAL":
        return DECIMAL
    if name in _FLOAT_TYPES:
        return FLOAT
    if name == "DATE":
        return DATE
    if name in _TIMESTAMP_TYPES:
        return TIMESTAMP
    if name in _TIME_TYPES:
        return TIME
    if name == "BOOLEAN":
        return BOOLEAN
    if name == "INTERVAL":
        return INTERVAL
    if name in _TEXT_TYPES:
        return TEXT
    return OTHER


def default_align(duck_type: str) -> str:
    """Numbers, dates, times and intervals right; text, booleans and the rest left."""
    return RIGHT if kind(duck_type) in _RIGHT_KINDS else LEFT


def plain(value: Any, duck_type: str = "") -> str:
    """The cell's text with no format: exact, ISO for dates and times, empty for NULL."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")  # never exponent form: 0E-20 shows its 20 places
    if isinstance(value, float):
        if duck_type.strip().upper() == "FLOAT":
            return _shortest_float32(value)
        return repr(value)
    if isinstance(value, datetime):
        return _trim_fraction(value.isoformat(sep=" "))
    if isinstance(value, (date, time)):
        return _trim_fraction(value.isoformat())
    if isinstance(value, timedelta):
        return str(value)
    return str(value)


def _shortest_float32(value: float) -> str:
    """A FLOAT (32-bit) arrives as the double nearest it (0.1 → 0.10000000149011612):
    show the shortest text that reads back as the same 32-bit value, as DuckDB does."""
    if value != value or value in (float("inf"), float("-inf")):
        return repr(value)
    for digits in range(1, 10):
        text = f"{value:.{digits}g}"
        if struct.unpack("f", struct.pack("f", float(text)))[0] == value:
            return repr(float(text))
    return repr(value)


_FRACTION_RE = re.compile(r"(\d\d:\d\d:\d\d)\.(\d+)")


def _trim_fraction(text: str) -> str:
    """Python pads microseconds to six places (``14:30:00.500000``); show the digits
    the value has, as DuckDB prints it (``14:30:00.5``)."""
    return _FRACTION_RE.sub(lambda m: f"{m.group(1)}.{m.group(2).rstrip('0')}", text)
