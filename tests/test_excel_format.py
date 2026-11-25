"""Excel format codes (the supported subset), rendered in Python.

CASES is the oracle: (code, DuckDB type, value) → (text, colour), the text being what
Excel shows. `~/dev/claude/work/strata/format-check/lo_check.py` (run from the repo)
cross-checks the cases against LibreOffice's TEXT() and lists each disagreement that is
not in EXCEL_NOT_LIBREOFFICE.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from strata.services import excel_format
from strata.services.excel_format import FormatError

D = Decimal
DT = datetime(2026, 11, 5, 14, 3, 9, 456789)  # a Thursday
DAY = date(2026, 11, 5)

# (code, duck_type, value, text, colour)
CASES: list[tuple[str, str, Any, str, str | None]] = [
    # placeholders and rounding (half away from zero)
    ("0", "DOUBLE", 1234.5, "1235", None),
    ("0", "DOUBLE", 2.5, "3", None),
    ("0", "DOUBLE", -2.5, "-3", None),
    ("0", "DOUBLE", 0.5, "1", None),
    ("0", "INTEGER", 0, "0", None),
    ("0.00", "DOUBLE", 1.005, "1.01", None),
    ("0.00", "DECIMAL(18,3)", D("12.345"), "12.35", None),
    ("0.00", "DECIMAL(38,20)", D("0E-20"), "0.00", None),
    ("0.00", "DOUBLE", -0.001, "-0.00", None),
    ("00000", "INTEGER", 42, "00042", None),
    ("000-000", "INTEGER", 123456, "123-456", None),
    ("#", "INTEGER", 0, "", None),
    ("#.##", "DOUBLE", 0.0, ".", None),
    ("#.##", "DOUBLE", 1.5, "1.5", None),
    ("#.##", "INTEGER", 1, "1.", None),
    ("0.0#", "DOUBLE", 1.5, "1.5", None),
    ("0.0#", "DOUBLE", 1.234, "1.23", None),
    ("0.0#", "INTEGER", 2, "2.0", None),
    ("0.0?", "DOUBLE", 1.5, "1.5\u2007", None),
    (".00", "DOUBLE", 0.5, ".50", None),
    (".00", "DOUBLE", 12.5, "12.50", None),
    # thousands and scaling
    ("#,##0", "INTEGER", 1234567, "1,234,567", None),
    ("#,##0", "INTEGER", 0, "0", None),
    ("#,##0", "INTEGER", -1234567, "-1,234,567", None),
    ("#,###", "INTEGER", 12, "12", None),
    ("#,###", "INTEGER", 1234, "1,234", None),
    ("0,000", "INTEGER", 5, "0,005", None),
    ("#,##0.00", "DECIMAL(18,2)", D("-1234.50"), "-1,234.50", None),
    ("#,##0.00", "DECIMAL(18,2)", D("999999.995"), "1,000,000.00", None),
    ("#,##0,", "INTEGER", 1234567, "1,235", None),
    ('0.0,,"M"', "INTEGER", 1234567, "1.2M", None),
    (
        "#,##0.00",
        "DECIMAL(38,20)",
        D("12345678901234567890.12345678901234567890"),
        "12,345,678,901,234,567,890.12",
        None,
    ),
    # percent
    ("0%", "DOUBLE", 0.256, "26%", None),
    ("0.0%", "DECIMAL(18,4)", D("0.1234"), "12.3%", None),
    ("0.00%", "DOUBLE", -0.05, "-5.00%", None),
    # sections and colours
    ("#,##0.00;[Red]-#,##0.00", "DECIMAL(18,2)", D("-1234.50"), "-1,234.50", "red"),
    ("#,##0.00;[Red]-#,##0.00", "DECIMAL(18,2)", D("1234.50"), "1,234.50", None),
    ("#,##0.00;[Red]-#,##0.00", "DECIMAL(18,2)", D("0"), "0.00", None),
    ("0.00;(0.00)", "INTEGER", -5, "(5.00)", None),
    ('0;-0;"zero"', "INTEGER", 0, "zero", None),
    ('0;-0;"zero"', "INTEGER", -3, "-3", None),
    ("0;;", "INTEGER", -5, "", None),
    ("0;;", "INTEGER", 0, "", None),
    ("0;;", "INTEGER", 5, "5", None),
    ("[Blue]0;[Magenta]0", "INTEGER", -5, "5", "fuchsia"),
    ("[Blue]0;[Magenta]0", "INTEGER", 5, "5", "blue"),
    ("[black]0", "INTEGER", 5, "5", "black"),
    ("[GREEN]0;[White]0", "INTEGER", -1, "1", "white"),
    ("General;[Red]-General", "DECIMAL(18,2)", D("-12.50"), "-12.50", "red"),
    # literal text
    ('"$"#,##0.00', "INTEGER", -5, "-$5.00", None),
    ("$#,##0.00", "DOUBLE", 1234.5, "$1,234.50", None),
    ("[$€-407] #,##0.00", "DOUBLE", 1234.5, "€ 1,234.50", None),
    ("[$-409]0.0", "DOUBLE", 1.25, "1.3", None),
    ("0.00_)", "INTEGER", 5, "5.00\u00a0", None),
    ('0.00" kg"', "INTEGER", 5, "5.00 kg", None),
    ("0\\h", "INTEGER", 5, "5h", None),
    ("*-0", "INTEGER", 5, "5", None),
    ("(0)", "INTEGER", 5, "(5)", None),
    # General = plain (Strata's default), whatever the type
    ("General", "DECIMAL(18,2)", D("12.50"), "12.50", None),
    ("general", "DATE", DAY, "2026-11-05", None),
    ("General", "BOOLEAN", True, "true", None),
    # dates
    ("yyyy-mm-dd", "DATE", DAY, "2026-11-05", None),
    ("d mmm yyyy", "DATE", DAY, "5 Nov 2026", None),
    ("dd/mm/yyyy", "DATE", DAY, "05/11/2026", None),
    ("ddd d mmm yyyy", "DATE", DAY, "Thu 5 Nov 2026", None),
    ("dddd, mmmm d, yyyy", "DATE", DAY, "Thursday, November 5, 2026", None),
    ("mmmmm yy", "DATE", DAY, "N 26", None),
    ("m/d/yy", "DATE", DAY, "11/5/26", None),
    ("YYYY-MM-DD", "DATE", DAY, "2026-11-05", None),
    ('"Week of "d mmm', "DATE", DAY, "Week of 5 Nov", None),
    ("[Red]yyyy", "DATE", DAY, "2026", "red"),
    ("yyyy-mm-dd hh:mm", "DATE", DAY, "2026-11-05 00:00", None),
    # timestamps and times
    ("yyyy-mm-dd hh:mm", "TIMESTAMP", DT, "2026-11-05 14:03", None),
    ("yyyy-mm-dd hh:mm:ss", "TIMESTAMP", DT, "2026-11-05 14:03:09", None),
    ("hh:mm:ss.0", "TIMESTAMP", DT, "14:03:09.5", None),
    ("hh:mm:ss.00", "TIMESTAMP", DT, "14:03:09.46", None),
    ("hh:mm:ss.000", "TIMESTAMP", DT, "14:03:09.457", None),
    ("h:mm AM/PM", "TIMESTAMP", DT, "2:03 PM", None),
    ("h:mm A/P", "TIMESTAMP", DT, "2:03 P", None),
    (
        "d mmm yyyy h:mm AM/PM",
        "TIMESTAMP",
        datetime(2026, 11, 5, 0, 5),
        "5 Nov 2026 12:05 AM",
        None,
    ),
    ("h AM/PM", "TIMESTAMP", datetime(2026, 11, 5, 12, 0), "12 PM", None),
    ("hh:mm", "TIMESTAMP", datetime(2026, 11, 5, 14, 30, 59, 900000), "14:30", None),
    ("hh:mm:ss", "TIMESTAMP", datetime(2026, 11, 5, 14, 30, 59, 600000), "14:31:00", None),
    (
        "yyyy-mm-dd hh:mm:ss",
        "TIMESTAMP",
        datetime(2026, 12, 31, 23, 59, 59, 600000),
        "2027-01-01 00:00:00",
        None,
    ),
    ("yyyy-mm-dd", "TIMESTAMP", datetime(2026, 11, 5, 23, 59, 59, 999999), "2026-11-05", None),
    ("h:m:s", "TIMESTAMP", datetime(2026, 11, 5, 9, 5, 7), "9:5:7", None),
    ("mm:ss", "TIME", time(0, 7, 5), "07:05", None),
    ("h:mm", "TIME", time(14, 30), "14:30", None),
    (
        "hh:mm",
        "TIMESTAMP WITH TIME ZONE",
        datetime(2026, 11, 5, 14, 30, tzinfo=timezone(timedelta(hours=10))),
        "14:30",
        None,
    ),
    # text
    ("@", "VARCHAR", "abc", "abc", None),
    ('"Item "@', "VARCHAR", "X1", "Item X1", None),
    ("[Blue]@", "VARCHAR", "x", "x", "blue"),
    # NULL is always empty
    ("#,##0.00", "DOUBLE", None, "", None),
    ("yyyy", "DATE", None, "", None),
]

# Cases LibreOffice cannot judge: General is Strata's plain text (Excel's General
# rounds to 11 digits), beyond-15-digit decimals, time zones, and Strata-only colours.
NOT_FOR_LO = {"General", "general", "General;[Red]-General"}

# Where LibreOffice's TEXT() differs from Excel (cross-checked 2026-11-25, LibreOffice
# on this host: the other 78 checked cases agree). Strata follows Excel, which draws the
# same codes in the xlsx download. Keyed by (code, repr(value)).
EXCEL_NOT_LIBREOFFICE = {
    ("0.00", "-0.001"): "Excel keeps the minus of a negative that rounds to zero (-0.00)",
    ("#.##", "0.0"): "Excel shows the point with no digits after it (.)",
    ("#.##", "1"): "Excel shows the point with no digits after it (1.)",
    ("h:mm A/P", repr(DT)): "Excel's A/P shows A or P as written; LibreOffice lowercases",
    ("hh:mm:ss", repr(datetime(2026, 11, 5, 14, 30, 59, 600000))): (
        "Excel rounds the seconds it shows; LibreOffice cuts them off"
    ),
    ("yyyy-mm-dd hh:mm:ss", repr(datetime(2026, 12, 31, 23, 59, 59, 600000))): (
        "Excel rounds the seconds it shows; LibreOffice cuts them off"
    ),
    ("yyyy-mm-dd", repr(datetime(2026, 11, 5, 23, 59, 59, 999999))): (
        "LibreOffice rounds the time to the millisecond first (next day); Strata shows the day"
    ),
    ("0.00_)", "5"): "_x is a no-break space in Strata (a plain one vanishes in HTML)",
}


@pytest.mark.parametrize(("code", "duck_type", "value", "text", "colour"), CASES)
def test_render(code: str, duck_type: str, value: Any, text: str, colour: str | None) -> None:
    assert excel_format.compile(code, duck_type).render(value) == (text, colour)


REFUSALS: list[tuple[str, str, str, int | None]] = [
    ("0.00E+00", "DOUBLE", "Scientific notation (E+) is not supported", 4),
    ("# ?/?", "DOUBLE", "Fractions (# ?/?) are not supported", 3),
    ("0/8", "DOUBLE", "Fractions (# ?/?) are not supported", 1),
    ("[>100]0", "DOUBLE", "Conditions such as [>100] are not supported", 0),
    ("[h]:mm", "TIMESTAMP", "Elapsed time such as [h] is not supported", 0),
    ("[Color10]0", "DOUBLE", "Numbered colours are not supported", 0),
    ("[Purple]0", "DOUBLE", "Unknown [Purple]: the colours are [Red]", 0),
    ("[Red][Blue]0", "DOUBLE", "Two colours in one section", 5),
    ("0 kg", "DOUBLE", "'k' must be in quotes (\"k\") or after a backslash (\\k)", 2),
    ("yyyy-mm-dd", "DOUBLE", "This column holds numbers: y, m, d, h and s", 0),
    ("0.00 AM/PM", "DOUBLE", "This column holds numbers", 5),
    ("#,##0", "DATE", "This column holds dates or times", 0),
    ('"x"', "DATE", "A date or time format needs y, m, d, h or s", 0),
    ("yyyy hh:mm", "TIME", "A TIME column has no date: use h, m and s", 0),
    ("hh:mm d", "TIME", "A TIME column has no date", 6),
    ("hh:mm:ss.0000", "TIME", "At most 3 places after the seconds", 12),
    ("yyyy;@", "DATE", "A date or time format has one section", 4),
    ("yyyy0", "DATE", "'0' does not belong in a date or time format", 4),
    ("0.00", "VARCHAR", 'A text column takes @ (e.g. "Item "@)', 0),
    ("@;@", "VARCHAR", "A text column takes @", 0),
    ("@ 0", "VARCHAR", "'0' does not belong in a text section", 2),
    ("@", "DOUBLE", "@ (the text) belongs in the fourth section only", 0),
    ("0;0;0;0;0", "DOUBLE", "At most 4 sections: positive;negative;zero;text", 7),
    ("0.0.0", "DOUBLE", "Only one decimal point per section", 3),
    ("0.0,0", "DOUBLE", "A comma among the decimal places", 3),
    ('"abc', "DOUBLE", 'A quoted text has no closing "', 0),
    ("[Red", "DOUBLE", "A [ has no closing ]", 0),
    ("0\\", "DOUBLE", "A backslash needs a character after it", 1),
    ("0_", "DOUBLE", "_ needs a character after it", 1),
    ("General 0", "DOUBLE", "General cannot be mixed with placeholders", 8),
    ('"x".', "DOUBLE", "A number section needs a placeholder (0 or #)", 3),
    ("", "DOUBLE", "A format cannot be empty", None),
    ("   ", "DOUBLE", "A format cannot be empty", None),
    (
        "0.00",
        "BOOLEAN",
        "Formats apply to numbers, dates, times and text; this column is BOOLEAN",
        None,
    ),
    (
        "0",
        "INTEGER[]",
        "Formats apply to numbers, dates, times and text; this column is INTEGER[]",
        None,
    ),
]


@pytest.mark.parametrize(("code", "duck_type", "message", "position"), REFUSALS)
def test_refused(code: str, duck_type: str, message: str, position: int | None) -> None:
    with pytest.raises(FormatError) as caught:
        excel_format.compile(code, duck_type)
    assert caught.value.message.startswith(message), caught.value.message
    assert caught.value.position == position


def test_the_message_names_the_character() -> None:
    with pytest.raises(FormatError, match=r"\(at character 3\)$"):
        excel_format.compile("0 kg", "DOUBLE")


def test_a_non_date_value_in_a_date_column_shows_plain() -> None:
    """Rendering never fails: a value of the wrong type shows as plain text."""
    assert excel_format.compile("yyyy", "DATE").render("2026") == ("2026", None)
    assert excel_format.compile("0.00", "DOUBLE").render("n/a") == ("n/a", None)


def test_not_a_number_shows_plain() -> None:
    fmt = excel_format.compile("#,##0.00", "DOUBLE")
    assert fmt.render(float("nan")) == ("nan", None)
    assert fmt.render(float("inf")) == ("inf", None)
