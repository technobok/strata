"""Reports blueprint — the public serve path for running a report macro.

``GET /r/<report_uuid>.<ext>`` renders (html) or streams (parquet/csv/json/xlsx)
a report over its datamodule's live version, rebuilding first if the datamodule
is TTL-stale. Query params: declared args by name, ``field__op=value`` filters,
``q`` substring search, ``sort=-a,b`` and ``page=N`` — all validated against
the macro's real output columns (services/query_filter). Authoring lives in
blueprints/datamodules.py; API-key access arrives in Phase 6.
"""

from typing import Any
from urllib.parse import urlencode

import duckdb
from flask import Blueprint, render_template, request, url_for
from flask import Response as FlaskResponse
from werkzeug.exceptions import NotFound

from strata.blueprints.auth import login_required
from strata.models.datamodule import Datamodule
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.services import datamodule_service, query_filter, report_service, stream_service
from strata.services.query_filter import FilterError, QuerySpec
from strata.services.report_service import ArgError, TableResult

bp = Blueprint("reports", __name__)

_SUPPORTED = frozenset({"html"}) | stream_service.STREAM_FORMATS

# Never propagated into generated (search/chip/sort/pager/export) URLs:
# page resets on any change; format/key are ignored placeholders.
_DROP_KEYS = ("page", "format", "key")


@bp.route("/r/<report_uuid>.<ext>")
@login_required
def serve(report_uuid: str, ext: str) -> FlaskResponse | str:
    resolved = report_service.resolve(report_uuid)
    if resolved is None:
        raise NotFound()
    report, dm = resolved

    if ext not in _SUPPORTED:
        return FlaskResponse(f"Unsupported format '{ext}'", status=400)

    pinned, version_error = _resolve_pinned(dm)

    args = ReportArg.get_for_report(report.id)
    arg_names = {a.name for a in args}
    pairs = list(request.args.items(multi=True))

    if ext != "html":
        if version_error is not None:
            return FlaskResponse(version_error, status=400)
        try:
            arg_values, spec = query_filter.parse_params(pairs, arg_names)
            return report_service.stream(report, dm, arg_values, spec, ext, pinned=pinned)
        except (ArgError, FilterError, duckdb.Error) as exc:
            return FlaskResponse(str(exc), status=400)

    options = report_service.arg_options_map(report, dm, args)

    # Run once args are submitted, or when nothing is required — a report with
    # required args shows its (options-populated) form before first running.
    result = None
    arg_error = version_error
    spec: QuerySpec | None = None
    arg_values: dict[str, list[str]] = {}
    if version_error is None:
        try:
            arg_values, spec = query_filter.parse_params(pairs, arg_names)
            if request.args or not any(a.required for a in args):
                result = report_service.run_table(report, dm, arg_values, spec, pinned=pinned)
        except (ArgError, FilterError) as exc:
            arg_error = str(exc)

    context = dict(
        report=report,
        datamodule=dm,
        args=args,
        arg_values=arg_values,
        options=options,
        result=result,
        arg_error=arg_error,
        pinned_version=pinned,
        ui=_build_ui(report, pairs, spec, result),
    )
    # HTMX swaps just the table fragment; history restores need the full page.
    if request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request"):
        return render_template("reports/_table.html", **context)
    return render_template("reports/view.html", **context)


def _resolve_pinned(dm: Datamodule) -> tuple[DatamoduleVersion | None, str | None]:
    """Resolve the reserved ``?version=`` pin. (None, None) = live serve.

    Unknown/foreign uuid or missing file -> NotFound; a non-servable status
    -> (None, message) for the caller (400 for streams, inline flash for html).
    """
    raw = request.args.get("version")
    if not raw:
        return None, None
    v = DatamoduleVersion.get_for_datamodule(dm.id, raw)
    if v is None:
        raise NotFound()
    if v.status not in ("ready", "superseded"):
        return None, f"Version {v.uuid[:8]} is not viewable (status: {v.status})"
    if not datamodule_service.version_path(dm.uuid, v.uuid).exists():
        raise NotFound()  # pruned or lost underneath us
    return v, None


def _serve_url(report: Report, ext: str, pairs: list[tuple[str, str]]) -> str:
    base = url_for("reports.serve", report_uuid=report.uuid, ext=ext)
    return f"{base}?{urlencode(pairs)}" if pairs else base


def _drop_first(pairs: list[tuple[str, str]], target: tuple[str, str]) -> list[tuple[str, str]]:
    out = list(pairs)
    try:
        out.remove(target)
    except ValueError:
        pass
    return out


def _build_ui(
    report: Report,
    pairs: list[tuple[str, str]],
    spec: QuerySpec | None,
    result: TableResult | None,
) -> dict[str, Any] | None:
    """Precompute the URLs/state the table fragment needs (search, chips, sort
    headers, pager, exports). Templates never assemble query strings.

    Chips are built from the raw spec, not the result, so a filter whose value
    fails to cast at runtime still shows up and can be removed.
    """
    if spec is None:
        return None
    keep = [(k, v) for k, v in pairs if k not in _DROP_KEYS]

    resolved = None
    if result is not None and len(result.applied_filters) == len(spec.filters):
        resolved = result.applied_filters
    chips = []
    for i, f in enumerate(spec.filters):
        label = resolved[i].label if resolved else f"{f.key} = {f.value}"
        chips.append(
            {"label": label, "url": _serve_url(report, "html", _drop_first(keep, (f.key, f.value)))}
        )
    for fragment in spec.q:
        chips.append(
            {
                "label": f"search: {fragment}",
                "url": _serve_url(report, "html", _drop_first(keep, ("q", fragment))),
            }
        )

    ui: dict[str, Any] = {
        "search_action": url_for("reports.serve", report_uuid=report.uuid, ext="html"),
        "search_hidden": keep,
        "chips": chips,
        "exports": [
            (fmt, _serve_url(report, fmt, keep)) for fmt in ("parquet", "csv", "json", "xlsx")
        ],
        "sort_state": {},
        "sort_urls": {},
        "pager": {"prev_url": None, "next_url": None},
        "back_to_live": (
            _serve_url(report, "html", [(k, v) for k, v in keep if k != "version"])
            if spec.version
            else None
        ),
    }
    if result is None or result.error:
        return ui

    sort_state = {t.field: ("desc" if t.desc else "asc") for t in result.applied_sort}
    ui["sort_state"] = sort_state
    base_no_sort = [(k, v) for k, v in keep if k != "sort"]
    for col in result.columns:
        # unsorted -> asc, asc -> desc, desc -> asc; a click replaces the whole
        # sort (multi-column stays URL-only).
        token = f"-{col}" if sort_state.get(col) == "asc" else col
        ui["sort_urls"][col] = _serve_url(report, "html", base_no_sort + [("sort", token)])

    if result.page > 1:
        prev = keep if result.page == 2 else keep + [("page", str(result.page - 1))]
        ui["pager"]["prev_url"] = _serve_url(report, "html", prev)
    if result.page < result.page_count:
        ui["pager"]["next_url"] = _serve_url(
            report, "html", keep + [("page", str(result.page + 1))]
        )
    return ui
