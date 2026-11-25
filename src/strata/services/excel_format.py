"""Excel number-format codes (a subset), rendered in Python on the rows shown.

``compile(code, duck_type)`` checks a code against a column's DuckDB type and returns a
``Formatter``; ``Formatter.render(value)`` gives ``(text, colour)``, where colour is a Pico
colour family (``red``, ``blue``, …, ``black``/``white`` for the theme's text and
background) or None. A code that is wrong, or uses something not supported, raises
``FormatError`` with a plain message and the position (0-based) of the offending part.
Nothing is ever silently ignored except what Excel itself ignores on screen (``*x`` fill).

Supported:

- numbers: ``0`` ``#`` ``?`` placeholders, ``.``, ``,`` (thousands between placeholders;
  each trailing ``,`` divides by 1,000), ``%`` (×100), ``General``;
- sections ``positive;negative;zero;text``: with two or more number sections the
  negative one shows the absolute value; with one, a negative value gets a leading ``-``;
- literal text: ``"…"``, ``\\x``, the characters ``$ - + / ( ) : ! ^ & ' ~ { } < > =`` and
  space, ``_x`` (a no-break space: a plain one would vanish at the end of an HTML cell),
  ``*x`` (ignored), ``[$X-nnn]`` (X); ``?`` pads with a figure space (as wide as a digit);
- colours ``[Red] [Blue] [Green] [Yellow] [Cyan] [Magenta] [Black] [White]``;
- dates and times: ``yy yyyy m mm mmm mmmm mmmmm d dd ddd dddd h hh m mm s ss`` with
  ``.0`` ``.00`` ``.000`` after seconds and ``AM/PM am/pm A/P a/p``; ``m``/``mm`` mean
  minutes right after an hour or right before seconds (Excel's rule). English names;
- text: ``@`` (the cell's text).

Refused for now: scientific ``E+``, fractions ``# ?/?``, conditions ``[>100]``, elapsed
``[h]``, ``[ColorN]``, other ``[…]``.

Rounding is half away from zero, as Excel rounds (``Decimal`` ROUND_HALF_UP; a float goes
through ``Decimal(repr(f))``). Seconds round to the places shown when seconds are shown;
a code without seconds cuts them off (14:30:59 shows 14:30 with ``hh:mm``).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from strata.services import cell_format

# Excel's eight named colours → Pico colour families (black/white = the theme's text
# and background, so they stay readable in dark mode).
COLOURS = {
    "red": "red",
    "blue": "blue",
    "green": "green",
    "yellow": "yellow",
    "cyan": "cyan",
    "magenta": "fuchsia",
    "black": "black",
    "white": "white",
}
_COLOUR_LIST = "[Red], [Blue], [Green], [Yellow], [Cyan], [Magenta], [Black] or [White]"

NBSP = "\u00a0"  # _x: kept at the end of an HTML cell, where a plain space collapses
FIGURE_SPACE = "\u2007"  # ?: a digit-wide space, so decimals line up (as LibreOffice pads)

# Characters Excel shows as themselves without quotes.
_LITERALS = frozenset("$-+/():!^&'~{}<>= ")

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class FormatError(ValueError):
    """A format code that cannot be used; ``position`` is 0-based (None = the whole code)."""

    def __init__(self, message: str, position: int | None = None) -> None:
        self.message = message
        self.position = position
        where = "" if position is None else f" (at character {position + 1})"
        super().__init__(message + where)


@dataclass
class _Tok:
    kind: str  # lit digit point comma percent at general date ampm colour
    text: str
    pos: int


@dataclass
class _Section:
    toks: list[_Tok] = field(default_factory=list)
    colour: str | None = None
    start: int = 0

    def has(self, *kinds: str) -> bool:
        return any(t.kind in kinds for t in self.toks)

    def first(self, *kinds: str) -> _Tok | None:
        return next((t for t in self.toks if t.kind in kinds), None)


def _lex(code: str) -> list[_Section]:
    sections = [_Section()]
    i, n = 0, len(code)

    def add(kind: str, text: str, pos: int) -> None:
        sections[-1].toks.append(_Tok(kind, text, pos))

    while i < n:
        ch = code[i]
        if ch == '"':
            end = code.find('"', i + 1)
            if end < 0:
                raise FormatError('A quoted text has no closing "', i)
            add("lit", code[i + 1 : end], i)
            i = end + 1
        elif ch == "\\":
            if i + 1 >= n:
                raise FormatError("A backslash needs a character after it", i)
            add("lit", code[i + 1], i)
            i += 2
        elif ch == "_":
            if i + 1 >= n:
                raise FormatError(
                    "_ needs a character after it (_) makes a space as wide as ')')", i
                )
            add("lit", NBSP, i)
            i += 2
        elif ch == "*":
            if i + 1 >= n:
                raise FormatError("* needs a character after it", i)
            i += 2  # Excel repeats it to fill the cell; a web table has nothing to fill
        elif ch == "[":
            end = code.find("]", i + 1)
            if end < 0:
                raise FormatError("A [ has no closing ]", i)
            _bracket(code[i + 1 : end], i, sections[-1])
            i = end + 1
        elif ch == ";":
            if len(sections) == 4:
                raise FormatError("At most 4 sections: positive;negative;zero;text", i)
            sections.append(_Section(start=i + 1))
            i += 1
        elif code[i : i + 7].lower() == "general":
            add("general", code[i : i + 7], i)
            i += 7
        elif ch in "0#?":
            add("digit", ch, i)
            i += 1
        elif ch == ".":
            add("point", ch, i)
            i += 1
        elif ch == ",":
            add("comma", ch, i)
            i += 1
        elif ch == "%":
            add("percent", ch, i)
            i += 1
        elif ch == "@":
            add("at", ch, i)
            i += 1
        elif ch in "Ee" and i + 1 < n and code[i + 1] in "+-":
            raise FormatError("Scientific notation (E+) is not supported", i)
        elif code[i : i + 5].upper() == "AM/PM":
            add("ampm", code[i : i + 5], i)
            i += 5
        elif code[i : i + 3].upper() == "A/P":
            add("ampm", code[i : i + 3], i)
            i += 3
        elif ch.lower() in "ymdhs":
            j = i
            while j < n and code[j].lower() == ch.lower():
                j += 1
            add("date", code[i:j].lower(), i)
            i = j
        elif ch == "/" and _is_fraction(code, i, sections[-1]):
            raise FormatError("Fractions (# ?/?) are not supported", i)
        elif ch in _LITERALS:
            add("lit", ch, i)
            i += 1
        else:
            raise FormatError(f'{ch!r} must be in quotes ("{ch}") or after a backslash (\\{ch})', i)
    return sections


def _is_fraction(code: str, i: int, section: _Section) -> bool:
    """A ``/`` between digit placeholders (``# ?/?``, ``0/8``) is a fraction."""
    before = [t for t in section.toks if not (t.kind == "lit" and t.text == " ")]
    after = code[i + 1 :].lstrip(" ")
    return bool(before) and before[-1].kind == "digit" and after[:1] in tuple("0123456789#?")


def _bracket(inner: str, pos: int, section: _Section) -> None:
    low = inner.lower()
    if low in COLOURS:
        if section.colour is not None:
            raise FormatError("Two colours in one section", pos)
        section.colour = COLOURS[low]
    elif inner.startswith("$"):
        text = inner[1:].split("-", 1)[0]  # [$€-x] → €; [$-409] (a locale) → nothing
        if text:
            section.toks.append(_Tok("lit", text, pos))
    elif re.fullmatch(r"[<>=].*", inner):
        raise FormatError("Conditions such as [>100] are not supported", pos)
    elif re.fullmatch(r"(?i)h+|m+|s+", inner):
        raise FormatError("Elapsed time such as [h] is not supported", pos)
    elif re.fullmatch(r"(?i)colou?r\s*\d+", inner):
        raise FormatError(f"Numbered colours are not supported: use {_COLOUR_LIST}", pos)
    else:
        raise FormatError(f"Unknown [{inner}]: the colours are {_COLOUR_LIST}", pos)


# ---------------------------------------------------------------------------
# Numbers


@dataclass
class _NumberSection:
    colour: str | None
    general: bool = False
    literal_only: bool = False
    int_slots: list[tuple[str, str]] = field(default_factory=list)  # ("d", "0#?") | ("l", text)
    frac_slots: list[tuple[str, str]] = field(default_factory=list)
    has_point: bool = False
    grouping: bool = False
    scale: int = 0  # trailing commas: ÷ 1000 each
    percent: int = 0
    lits: list[tuple[str, str]] = field(default_factory=list)  # for literal-only / General

    @property
    def places(self) -> int:
        return sum(1 for k, _ in self.frac_slots if k == "d")

    def render(self, value: Decimal, sign: str) -> str:
        if self.general:
            body = "".join(cell_format.plain(value) if k == "g" else text for k, text in self.lits)
            return sign + body
        if self.literal_only:
            return sign + "".join(text for _, text in self.lits)
        with localcontext() as ctx:
            ctx.prec = 200  # a DECIMAL(38,20) has 38 digits; the default context keeps 28
            v = value * (Decimal(100) ** self.percent) / (Decimal(1000) ** self.scale)
            v = v.quantize(Decimal(1).scaleb(-self.places), rounding=ROUND_HALF_UP)
        digits = format(v, "f")
        whole, _, frac = digits.partition(".")
        whole = whole.lstrip("0")
        return sign + self._whole(whole) + ("." if self.has_point else "") + self._frac(frac)

    def _whole(self, digits: str) -> str:
        slots = self.int_slots
        positions = [i for i, (k, _) in enumerate(slots) if k == "d"]
        out: list[str] = [text if k == "l" else "" for k, text in slots]
        if not positions:
            # No placeholders before the point (".00"): the integer digits, if any, sit
            # just before it.
            return "".join(out) + self._grouped(digits)
        for r, idx in enumerate(reversed(positions)):
            if r < len(digits):
                out[idx] = digits[len(digits) - 1 - r]
            else:
                out[idx] = {"0": "0", "#": "", "?": FIGURE_SPACE}[slots[idx][1]]
        extra = digits[: max(0, len(digits) - len(positions))]
        out[positions[0]] = extra + out[positions[0]]
        if self.grouping:
            out = self._group_pieces(out, positions)
        return "".join(out)

    def _grouped(self, digits: str) -> str:
        if not self.grouping or not digits:
            return digits
        return f"{int(digits):,}"

    @staticmethod
    def _group_pieces(out: list[str], positions: list[int]) -> list[str]:
        """Thousands commas between the shown digits, counted from the right."""
        chars: list[tuple[int, str]] = []  # (slot index, digit) for every shown digit
        for idx in positions:
            chars += [(idx, c) for c in out[idx] if c.isdigit()]
        marks = {len(chars) - 1 - r for r in range(3, len(chars), 3)}
        rebuilt = {idx: "" for idx in positions}
        for n, (idx, c) in enumerate(chars):
            rebuilt[idx] += c + ("," if n in marks else "")
        for idx in positions:
            # keep a padding space (from ?) where no digit landed
            out[idx] = rebuilt[idx] or out[idx]
        return out

    def _frac(self, digits: str) -> str:
        slots = self.frac_slots
        out: list[str] = []
        d = iter(digits)
        for k, text in slots:
            out.append(next(d, "0") if k == "d" else text)
        # Trailing zeros: '#' drops them, '?' turns them into spaces, '0' keeps them.
        for i in range(len(slots) - 1, -1, -1):
            k, ph = slots[i]
            if k == "l":
                continue
            if out[i] != "0" or ph == "0":
                break
            out[i] = "" if ph == "#" else FIGURE_SPACE
        return "".join(out)


def _number_section(section: _Section) -> _NumberSection:
    ns = _NumberSection(colour=section.colour)
    toks = section.toks
    if any(t.kind == "general" for t in toks):
        ns.general = True
        for t in toks:
            if t.kind not in ("general", "lit"):
                raise FormatError("General cannot be mixed with placeholders", t.pos)
            ns.lits.append(("g" if t.kind == "general" else "l", t.text))
        return ns
    digit_idx = [i for i, t in enumerate(toks) if t.kind == "digit"]
    if not digit_idx:
        if section.has("point", "comma", "percent"):
            bad = section.first("point", "comma", "percent")
            assert bad is not None
            raise FormatError("A number section needs a placeholder (0 or #)", bad.pos)
        ns.literal_only = True
        ns.lits = [("l", t.text) for t in toks]
        return ns
    last_digit = digit_idx[-1]
    j = last_digit + 1
    while j < len(toks) and toks[j].kind == "comma":
        ns.scale += 1
        j += 1
    scaling = set(range(last_digit + 1, j))
    points = [i for i, t in enumerate(toks) if t.kind == "point"]
    if len(points) > 1:
        raise FormatError("Only one decimal point per section", toks[points[1]].pos)
    split = points[0] if points else len(toks)
    ns.has_point = bool(points)
    for i, t in enumerate(toks):
        if i in scaling:
            continue
        target = ns.int_slots if i < split else ns.frac_slots
        if t.kind == "digit":
            target.append(("d", t.text))
        elif t.kind == "lit":
            target.append(("l", t.text))
        elif t.kind == "percent":
            ns.percent += 1
            target.append(("l", "%"))
        elif t.kind == "comma":
            if i > split:
                raise FormatError("A comma among the decimal places", t.pos)
            ns.grouping = True
    return ns


# ---------------------------------------------------------------------------
# Dates and times


@dataclass
class _DatePart:
    kind: str  # lit year month day hour minute second frac ampm
    width: int = 0
    text: str = ""


@dataclass
class _DateSection:
    colour: str | None
    parts: list[_DatePart]
    twelve_hour: bool
    seconds: bool
    places: int  # digits after the seconds

    def render(self, value: datetime) -> str:
        if self.seconds:
            q = 10 ** (6 - self.places)
            us = (value.microsecond + q // 2) // q * q
            value = value.replace(microsecond=0) + timedelta(microseconds=us)
        out: list[str] = []
        for p in self.parts:
            out.append(_date_part(p, value, self.twelve_hour))
        return "".join(out)


def _date_part(p: _DatePart, v: datetime, twelve: bool) -> str:
    match p.kind:
        case "lit":
            return p.text
        case "year":
            return f"{v.year % 100:02d}" if p.width <= 2 else f"{v.year:04d}"
        case "month":
            if p.width == 1:
                return str(v.month)
            if p.width == 2:
                return f"{v.month:02d}"
            name = MONTHS[v.month - 1]
            return name[:3] if p.width == 3 else name[0] if p.width == 5 else name
        case "day":
            if p.width == 1:
                return str(v.day)
            if p.width == 2:
                return f"{v.day:02d}"
            name = DAYS[v.weekday()]
            return name[:3] if p.width == 3 else name
        case "hour":
            h = (v.hour % 12 or 12) if twelve else v.hour
            return str(h) if p.width == 1 else f"{h:02d}"
        case "minute":
            return str(v.minute) if p.width == 1 else f"{v.minute:02d}"
        case "second":
            return str(v.second) if p.width == 1 else f"{v.second:02d}"
        case "frac":
            return "." + f"{v.microsecond:06d}"[: p.width]
        case _:  # ampm
            am, pm = p.text.split("/")
            return am if v.hour < 12 else pm


def _date_section(section: _Section, kind: str) -> _DateSection:
    toks = section.toks
    parts: list[_DatePart] = []
    seconds = False
    places = 0
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == "date":
            letter, width = t.text[0], len(t.text)
            name = {"y": "year", "m": "month", "d": "day", "h": "hour", "s": "second"}[letter]
            parts.append(_DatePart(name, width, t.text))
            if letter == "s":
                seconds = True
                # .0 .00 .000 right after the seconds
                if i + 1 < len(toks) and toks[i + 1].kind == "point":
                    j = i + 2
                    while j < len(toks) and toks[j].kind == "digit" and toks[j].text == "0":
                        j += 1
                    if j > i + 2:
                        places = j - i - 2
                        if places > 3:
                            raise FormatError("At most 3 places after the seconds", toks[i + 5].pos)
                        parts.append(_DatePart("frac", places))
                        i = j
                        continue
        elif t.kind == "ampm":
            parts.append(_DatePart("ampm", text=t.text))
        elif t.kind == "lit":
            parts.append(_DatePart("lit", text=t.text))
        elif t.kind in ("point", "comma"):
            parts.append(_DatePart("lit", text=t.text))
        else:
            raise FormatError(f"{t.text!r} does not belong in a date or time format", t.pos)
        i += 1
    # Excel's rule: m/mm right after an hour, or right before seconds, is minutes.
    clock = [p for p in parts if p.kind in ("year", "month", "day", "hour", "second")]
    for n, p in enumerate(clock):
        if p.kind == "month" and p.width <= 2:
            after_hour = n > 0 and clock[n - 1].kind == "hour"
            before_second = n + 1 < len(clock) and clock[n + 1].kind == "second"
            if after_hour or before_second:
                p.kind = "minute"
    if kind == cell_format.TIME:
        dated = next((p for p in parts if p.kind in ("year", "month", "day")), None)
        if dated is not None:
            pos = next(t.pos for t in toks if t.kind == "date" and t.text == dated.text)
            raise FormatError("A TIME column has no date: use h, m and s", pos)
    return _DateSection(
        colour=section.colour,
        parts=parts,
        twelve_hour=any(p.kind == "ampm" for p in parts),
        seconds=seconds,
        places=places,
    )


# ---------------------------------------------------------------------------
# Text


@dataclass
class _TextSection:
    colour: str | None
    parts: list[tuple[str, str]]  # ("@", "") or ("l", text)

    def render(self, value: str) -> str:
        return "".join(value if k == "@" else text for k, text in self.parts)


def _text_section(section: _Section) -> _TextSection:
    parts: list[tuple[str, str]] = []
    for t in section.toks:
        if t.kind == "at":
            parts.append(("@", ""))
        elif t.kind == "lit":
            parts.append(("l", t.text))
        else:
            raise FormatError(f"{t.text!r} does not belong in a text section (with @)", t.pos)
    return _TextSection(colour=section.colour, parts=parts)


# ---------------------------------------------------------------------------


@dataclass
class Formatter:
    """A compiled code for one column type. ``render(value)`` → (text, colour or None)."""

    code: str
    duck_type: str
    general: bool = False
    numbers: list[_NumberSection | None] = field(default_factory=list)  # None = empty
    date: _DateSection | None = None
    text: _TextSection | None = None

    def render(self, value: Any) -> tuple[str, str | None]:
        if value is None:
            return "", None
        if self.general:
            return cell_format.plain(value, self.duck_type), None
        if self.date is not None and isinstance(value, (date, time)):
            return self.date.render(_as_datetime(value)), self.date.colour
        if self.text is not None and isinstance(value, str):
            return self.text.render(value), self.text.colour
        if (
            self.numbers
            and isinstance(value, (int, float, Decimal))
            and not isinstance(value, bool)
        ):
            return self._number(value)
        return cell_format.plain(value, self.duck_type), None

    def _number(self, value: int | float | Decimal) -> tuple[str, str | None]:
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return cell_format.plain(value, self.duck_type), None
            value = Decimal(repr(value))
        d = Decimal(value)
        secs = self.numbers
        if len(secs) == 1:
            sec, sign = secs[0], "-" if d < 0 else ""
        elif d < 0:
            sec, sign = secs[1], ""
        elif d == 0 and len(secs) >= 3:
            sec, sign = secs[2], ""
        else:
            sec, sign = secs[0], ""
        if sec is None:  # an empty section shows nothing
            return "", None
        return sec.render(abs(d), sign), sec.colour


def _as_datetime(value: date | time) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.combine(date(1899, 12, 30), value)  # Excel's day 0, for a TIME


def check_syntax(code: str) -> None:
    """Check what ``code`` can be checked for without its column's type: its syntax and
    what is not supported. FormatError as ``compile``."""
    if not code.strip():
        raise FormatError("A format cannot be empty")
    _lex(code)


def compile(code: str, duck_type: str) -> Formatter:  # noqa: A001 - the module's verb
    """Check ``code`` against a column of ``duck_type`` and compile it; FormatError
    says what is wrong and where."""
    if not code.strip():
        raise FormatError("A format cannot be empty")
    kind = cell_format.kind(duck_type)
    sections = _lex(code)
    fmt = Formatter(code=code, duck_type=duck_type)
    if len(sections) == 1 and [t.kind for t in sections[0].toks] == ["general"]:
        if sections[0].colour is None:
            fmt.general = True
            return fmt
    if kind in cell_format.NUMBER_KINDS:
        _compile_numbers(fmt, sections)
    elif kind in cell_format.DATE_TIME_KINDS:
        if len(sections) > 1:
            raise FormatError("A date or time format has one section", sections[1].start - 1)
        section = sections[0]
        number = section.first("digit", "percent", "general", "at")
        if number is not None and not section.has("date"):
            raise FormatError("This column holds dates or times: use y, m, d, h and s", number.pos)
        if not section.has("date", "ampm"):
            raise FormatError("A date or time format needs y, m, d, h or s", section.start)
        fmt.date = _date_section(section, kind)
    elif kind == cell_format.TEXT:
        if len(sections) > 1 or not sections[0].has("at"):
            raise FormatError('A text column takes @ (e.g. "Item "@)', 0)
        fmt.text = _text_section(sections[0])
    else:
        raise FormatError(
            f"Formats apply to numbers, dates, times and text; this column is {duck_type}"
        )
    return fmt


def _compile_numbers(fmt: Formatter, sections: list[_Section]) -> None:
    numbers: list[_NumberSection | None] = []
    for n, section in enumerate(sections):
        date_tok = section.first("date", "ampm")
        if date_tok is not None:
            raise FormatError(
                "This column holds numbers: y, m, d, h and s are for dates and times",
                date_tok.pos,
            )
        at = section.first("at")
        if at is not None:
            if n != 3:
                raise FormatError("@ (the text) belongs in the fourth section only", at.pos)
            continue  # a number never reaches the text section
        if not section.toks and n > 0:
            numbers.append(None)  # 0;;  → negatives show nothing
            continue
        numbers.append(_number_section(section))
    if len(numbers) == 4:
        numbers = numbers[:3]
    if numbers[0] is None:
        numbers[0] = _NumberSection(colour=None, literal_only=True)
    fmt.numbers = numbers
