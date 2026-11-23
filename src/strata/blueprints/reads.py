"""The read audit's pages: ``/admin/reads`` (admins, everything) and
``/datamodules/<uuid>/reads`` (a datamodule's owners, its reads only), each with a
CSV download of the same filter. The routes live in the admin and datamodules
blueprints; this module parses the filter and renders both.
"""

import csv
import io
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

from flask import Response, render_template, request, stream_with_context

from strata.models.api_key import ApiKey
from strata.models.datamodule import Datamodule
from strata.models.read_log import ReadFilter, ReadLog
from strata.services.read_audit import KINDS

FILTER_KEYS = ("dm", "report", "user", "key", "kind", "refused", "from", "to")


def _int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() else None


def _date(raw: str | None, label: str, problems: list[str]) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        problems.append(f"{label} '{raw}' is not a date (YYYY-MM-DD); it is ignored.")
        return None


def parse_filter(fixed: Datamodule | None) -> tuple[ReadFilter, list[str]]:
    """The filter in the query string. ``fixed`` = the owners' page: always that
    datamodule, whatever the URL says."""
    args = request.args
    problems: list[str] = []
    kind = args.get("kind") or None
    if kind is not None and kind not in KINDS:
        problems.append(f"Unknown kind '{kind}'; it is ignored.")
        kind = None
    filters = ReadFilter(
        datamodule_id=fixed.id if fixed is not None else _int(args.get("dm")),
        report_id=_int(args.get("report")),
        username=(args.get("user") or "").strip() or None,
        api_key_id=_int(args.get("key")),
        kind=kind,
        refused=args.get("refused") == "1",
        since=_date(args.get("from"), "From", problems),
        until=_date(args.get("to"), "To", problems),
    )
    return filters, problems


def _who(row: ReadLog) -> str:
    if row.username:
        return row.username
    if row.api_key_id is not None:
        return f"key {row.key_name or row.api_key_id} ({row.token_prefix or '?'}…)"
    if row.token_prefix:
        return f"unknown key ({row.token_prefix}…)"
    return "anonymous"


def render(fixed: Datamodule | None, url: Callable[..., str]) -> str:
    """The page. ``url(**query)`` builds this page's own URL (or its CSV's, with
    ``csv=True``)."""
    filters, problems = parse_filter(fixed)
    rows, more = ReadLog.search(filters, before_id=_int(request.args.get("before")))
    kept = [(k, v) for k, v in request.args.items(multi=True) if k in FILTER_KEYS and v]
    key = ApiKey.get_by_id(filters.api_key_id) if filters.api_key_id is not None else None
    dm_id = filters.datamodule_id
    return render_template(
        "reads/index.html",
        fixed=fixed,
        rows=[(row, _who(row)) for row in rows],
        filters=filters,
        problems=problems,
        kinds=KINDS,
        datamodules=ReadLog.datamodules() if fixed is None else [],
        reports=ReadLog.reports(dm_id) if dm_id is not None else [],
        key=key,
        key_filter=filters.api_key_id,
        without_key=url(**_query([(k, v) for k, v in kept if k != "key"])),
        newest=url(**_query(kept)) if request.args.get("before") else None,
        older=url(**_query([*kept, ("before", str(rows[-1].id))])) if more and rows else None,
        csv_url=url(csv=True, **_query(kept)),
        action=url(),
    )


def _query(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """url_for keyword arguments from query pairs (the last of a repeated key)."""
    return dict(pairs)


def csv_response(fixed: Datamodule | None, filename: str) -> Response:
    filters, _problems = parse_filter(fixed)

    def lines() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(ReadLog.FIELDS)
        for row in ReadLog.iterate(filters):
            writer.writerow([getattr(row, name) for name in ReadLog.FIELDS])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
        yield buffer.getvalue()

    return Response(
        stream_with_context(lines()),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
