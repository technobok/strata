"""Reports blueprint — the public serve path for running a report macro.

``GET /r/<report_uuid>.<ext>`` renders (html) or streams (parquet/csv/json/xlsx)
a report over its datamodule's live version, rebuilding first if the datamodule
is TTL-stale. Query params: declared args by name, ``field__op=value`` filters,
``q`` substring search, ``sort=-a,b`` and ``page=N`` — all validated against
the macro's real output columns (services/query_filter). Auth: a session user,
or for data formats an API key via ``?key=<token>`` or an ``Authorization:
Bearer <token>`` header (hashed lookup, scope + expiry checked; html always
requires a session). Every request that names a
report is recorded by the read audit (services/read_audit). Authoring
lives in blueprints/datamodules.py.
"""

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import duckdb
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask import Response as FlaskResponse
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Response as WerkzeugResponse

from strata.blueprints.auth import forbid, login_required
from strata.models.api_key import ApiKey, hash_token
from strata.models.datamodule import Datamodule
from strata.models.datamodule_version import DatamoduleVersion
from strata.models.report import Report
from strata.models.report_arg import ReportArg
from strata.services import (
    access,
    build_queue,
    datamodule_service,
    query_filter,
    read_audit,
    report_service,
    stream_service,
)
from strata.services.query_filter import FilterError, QuerySpec
from strata.services.report_service import ArgError, TableResult

log = logging.getLogger(__name__)

bp = Blueprint("reports", __name__)

_SUPPORTED = frozenset({"html"}) | stream_service.STREAM_FORMATS

NOT_A_VIEWER = "You are not one of this report's viewers; its owners can add you."

# Never propagated into generated (search/chip/sort/pager/export) URLs:
# page resets on any change; format/key are ignored placeholders.
_DROP_KEYS = ("page", "format", "key")


@bp.route("/r/<report_uuid>.<ext>")
def serve(report_uuid: str, ext: str) -> FlaskResponse | WerkzeugResponse | str:
    resolved = report_service.resolve(report_uuid)
    if resolved is None:
        raise NotFound()
    report, dm = resolved
    # The read audit records this request from here on, refusals included.
    read_audit.note("page" if ext == "html" else "data", dm, report, format=ext[:16])

    if ext not in _SUPPORTED:
        return FlaskResponse(f"Unsupported format '{ext}'", status=400)
    # Before anything that could start a build: a non-viewer never refreshes data.
    if g.get("user") is not None and not access.can_view(dm):
        if ext == "html":
            forbid(NOT_A_VIEWER)
        return FlaskResponse(NOT_A_VIEWER, status=403)
    # A locked datamodule serves nothing to anyone: pages, data, admin keys, Excel links.
    if dm.access_lock:
        return _locked(dm, ext)

    # Session users pass straight through; anonymous callers need a valid key
    # (?key= or a Bearer header) and get data formats only (html keeps the SSO
    # redirect and never reads a key).
    api_key: ApiKey | None = None
    if g.get("user") is None:
        if ext == "html":
            # Never carry an API key into the login redirect (it would land in
            # gatekeeper's URL and logs); the key cannot open html anyway.
            query = urlencode([(k, v) for k, v in request.args.items(multi=True) if k != "key"])
            next_url = request.base_url + (f"?{query}" if query else "")
            return redirect(url_for("auth.login", next=next_url))
        api_key, deny = _authenticate_key(report, ext)
        if deny is not None:
            return deny

    pinned, version_error = _resolve_pinned(dm)

    # The serve path never builds: a stale datamodule gets a rebuild request and
    # a bounded wait, then the last good version is served, marked stale.
    served: build_queue.Served | None = None
    if pinned is None and version_error is None:
        served = build_queue.servable_version(dm, dm.ttl_seconds)
        # The rebuild it may have waited for can have locked the datamodule.
        current = Datamodule.get_by_id(dm.id)
        if current is None or current.access_lock:
            return _locked(current or dm, ext)
        if served.version is None:
            return _not_built_yet(report, dm, served, ext)
    version = pinned if pinned is not None else (served.version if served else None)
    if version is not None:
        read_audit.update(version=version)

    args = ReportArg.get_for_report(report.id)
    arg_names = {a.name for a in args}
    pairs = list(request.args.items(multi=True))

    if ext != "html":
        if version_error is not None or version is None:
            return FlaskResponse(version_error or "no servable version", status=400)
        try:
            arg_values, spec = query_filter.parse_params(pairs, arg_names)
            if api_key is not None:
                arg_values = _apply_key_args(api_key, arg_values)
            resp = report_service.stream(report, dm, arg_values, spec, ext, version)
            _freshness_headers(resp, version, served)
        except (ArgError, FilterError, duckdb.Error) as exc:
            return FlaskResponse(str(exc), status=400)
        if api_key is not None:
            api_key.record_use()
        return resp

    options = report_service.arg_options_map(report, dm, args)

    # Run once args are submitted, or when nothing is required — a report with
    # required args shows its (options-populated) form before first running.
    result = None
    arg_error = version_error
    spec: QuerySpec | None = None
    arg_values: dict[str, list[str]] = {}
    if version_error is None and version is not None:
        try:
            arg_values, spec = query_filter.parse_params(pairs, arg_names)
            if request.args or not any(a.required for a in args):
                result = report_service.run_table(report, dm, arg_values, spec, version)
                read_audit.update(row_count=result.row_count)
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
        served=served,
        served_version=version,
        ui=build_ui(
            lambda ext, p: _serve_url(report, ext, p),
            pairs,
            spec,
            result,
            excel=lambda p: _excel_box_url(report, p),
        ),
    )
    # HTMX swaps just the table fragment; history restores need the full page.
    if request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request"):
        return render_template("reports/_table.html", **context)
    return render_template("reports/view.html", **context)


def _locked(dm: Datamodule, ext: str) -> FlaskResponse:
    """403 for a locked datamodule. The reason names people: its owners and the
    admins see it, everyone else (and every key) only that it is locked."""
    if ext != "html":
        return FlaskResponse(access.LOCKED, status=403)
    reason = dm.access_lock if g.get("user") is not None and access.can_own(dm) else None
    forbid(f"{access.LOCKED} {reason}" if reason else access.LOCKED)


def _freshness_headers(
    resp: FlaskResponse, version: DatamoduleVersion, served: build_queue.Served | None
) -> None:
    """Tell data consumers how old the data is (PowerQuery can read headers)."""
    if version.build_ended_at:
        resp.headers["X-Data-As-Of"] = version.build_ended_at
    if served is not None and served.stale:
        resp.headers["X-Data-Stale"] = "1"


def _not_built_yet(
    report: Report,
    dm: Datamodule,
    served: build_queue.Served,
    ext: str,
) -> FlaskResponse | str:
    """Nothing has ever been built: a self-refreshing page, or 503 for data."""
    if ext == "html":
        html = render_template("reports/building.html", report=report, datamodule=dm, served=served)
        return FlaskResponse(html, status=503, headers={"Retry-After": "30"})
    message = (
        f"The data for this report could not be built: {served.failure}"
        if served.failure
        else "The data for this report is still being built; retry shortly."
    )
    return FlaskResponse(message, status=503, headers={"Retry-After": "30"})


TWO_KEYS = (
    "two different keys: send the key in the link (?key=) or in the Authorization header, not both"
)


def _sent_token() -> tuple[str, FlaskResponse | None]:
    """The key a data request carries: ``?key=<token>`` or ``Authorization: Bearer
    <token>`` (another scheme, or Power Query's empty ``Bearer``, is no key). Both
    may come only if they are the same token; two different ones get 400."""
    in_link = request.args.get("key", "")
    scheme, _, credentials = request.headers.get("Authorization", "").strip().partition(" ")
    in_header = credentials.strip() if scheme.lower() == "bearer" else ""
    if in_link and in_header and in_link != in_header:
        return "", FlaskResponse(TWO_KEYS, status=400)
    return in_link or in_header, None


def _authenticate_key(report: Report, ext: str) -> tuple[ApiKey | None, FlaskResponse | None]:
    """Anonymous data request: authenticate its key (``_sent_token``).

    (key, None) on success; (None, 400/401/403 response) otherwise. The 403 text
    is uniform — it never reveals whether the token, expiry, or scope failed — but
    the read audit records the key it matched, or the prefix of a token that
    matched none.
    """
    token, deny = _sent_token()
    if deny is not None:
        return None, deny
    if not token:
        return None, FlaskResponse("authentication required", status=401)
    key = ApiKey.get_by_token_hash(hash_token(token))
    if key is None:
        read_audit.update(token_prefix=token[:8])
        return None, FlaskResponse("invalid API key", status=403)
    read_audit.update(key=key)
    in_scope = (key.scope_type == "report" and key.report_id == report.id) or (
        key.scope_type == "datamodule" and key.datamodule_id == report.datamodule_id
    )
    if not key.is_valid() or not in_scope:
        return None, FlaskResponse("invalid API key", status=403)
    return key, None


def _apply_key_args(key: ApiKey, arg_values: dict[str, list[str]]) -> dict[str, list[str]]:
    """Key arg-locking: the whitelist filters caller args, then fixed args
    overlay (fixed always wins). Absent args fall to report defaults in
    _bind_args; filters/sort/page/q/version were already partitioned out.
    """
    allowed = key.parameterised_args()
    if allowed is not None:
        allowed_set = set(allowed)
        arg_values = {k: v for k, v in arg_values.items() if k in allowed_set}
    else:
        arg_values = dict(arg_values)
    for name, value in key.fixed_args().items():
        values = value if isinstance(value, list) else [value]
        arg_values[name] = [str(v) for v in values]
    return arg_values


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


# ---------------------------------------------------------------------------
# "Use in Excel": the report's Excel link for the current view
# ---------------------------------------------------------------------------

# The first is the box's default. Excel's From Web opens xlsx, CSV and JSON as a
# table, but not Parquet: Power Query reads Parquet only from local files and Azure
# storage (tried 2026-11-23: a .parquet link loads as an unopened file).
EXCEL_FORMATS = (
    ("xlsx", "Excel workbook (.xlsx)"),
    ("csv", "CSV"),
    ("json", "JSON"),
    ("parquet", "Parquet (for Power BI and other tools; Excel cannot open it from a link)"),
)


def _excel_view(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The view an Excel link keeps: arguments, filters, sort and search. Never a
    page, a pinned version (a workbook refreshes to live data) or a key."""
    return [(k, v) for k, v in pairs if k not in (*_DROP_KEYS, "version")]


def _excel_box_url(report: Report, pairs: list[tuple[str, str]]) -> str:
    base = url_for("reports.excel_box", report_uuid=report.uuid)
    return f"{base}?{urlencode(pairs)}" if pairs else base


def _excel_box(report: Report, dm: Datamodule, key: ApiKey, replaced: bool = False) -> str:
    view = _excel_view(list(request.args.items(multi=True)))
    query = urlencode([*view, ("key", key.link_token())])
    links = [
        (
            label,
            url_for("reports.serve", report_uuid=report.uuid, ext=fmt, _external=True)
            + "?"
            + query,
        )
        for fmt, label in EXCEL_FORMATS
    ]
    return render_template(
        "reports/_excel.html",
        datamodule=dm,
        links=links,
        replaced=replaced,
        key_enabled=key.enabled,
        # Replacing breaks every workbook on the report: its datamodule's owners only.
        can_replace=access.can_own(dm),
        replace_url=url_for("reports.excel_replace", report_uuid=report.uuid)
        + (f"?{urlencode(view)}" if view else ""),
    )


@bp.route("/r/<report_uuid>/excel")
@login_required
def excel_box(report_uuid: str) -> str:
    """HTMX: the report's Excel link for the view in the query string. Its key is the
    datamodule's Excel link, made the first time anyone opens a box on it."""
    resolved = report_service.resolve(report_uuid)
    if resolved is None:
        raise NotFound()
    report, dm = resolved
    read_audit.note("excel-key", dm, report)  # a key is shown to this person
    if not access.can_view(dm):
        forbid(NOT_A_VIEWER)
    if dm.access_lock:
        forbid(access.LOCKED)
    key = ApiKey.ensure_excel_link(dm.id, g.user.username)
    read_audit.update(key=key)
    return _excel_box(report, dm, key)


@bp.route("/r/<report_uuid>/excel/replace", methods=["POST"])
@login_required
def excel_replace(report_uuid: str) -> str | FlaskResponse:
    """HTMX: a new token for the datamodule's Excel link; every workbook on any of
    its reports (old per-report links too) stops refreshing."""
    resolved = report_service.resolve(report_uuid)
    if resolved is None:
        raise NotFound()
    report, dm = resolved
    read_audit.note("excel-key", dm, report)  # the new key is shown to this person
    if not access.can_own(dm):
        return FlaskResponse(
            "Only the owners of this report's datamodule and the Strata admins can replace "
            "its key.",
            status=403,
        )
    key = ApiKey.ensure_excel_link(dm.id, g.user.username)
    key.rotate()
    read_audit.update(key=key)
    return _excel_box(report, dm, key, replaced=True)


@bp.route("/r/<report_uuid>/build", methods=["POST"])
@login_required
def build_now(report_uuid: str) -> WerkzeugResponse:
    """A viewer's "Build now": rebuild the report's datamodule (its dependencies only
    if they are stale; "with all dependencies" is for owners, on the datamodule's
    page), then back to the report."""
    resolved = report_service.resolve(report_uuid)
    if resolved is None:
        raise NotFound()
    report, dm = resolved
    if not access.can_view(dm):
        forbid(NOT_A_VIEWER)
    if dm.access_lock:
        forbid(f"{access.LOCKED} Its owners rebuild it from its page once it is fixed.")
    job = build_queue.request_build(dm, f"manual:{g.user.username}", force=True)
    if job.status == "done":
        flash("Build finished.", "success")
    elif job.status == "failed":
        flash(f"Build failed: {job.error}", "error")
    else:
        flash("Build queued; the worker will run it.", "success")
    return redirect(url_for("reports.serve", report_uuid=report.uuid, ext="html"))


def _drop_first(pairs: list[tuple[str, str]], target: tuple[str, str]) -> list[tuple[str, str]]:
    out = list(pairs)
    try:
        out.remove(target)
    except ValueError:
        pass
    return out


def build_ui(
    url: Callable[[str, list[tuple[str, str]]], str],
    pairs: list[tuple[str, str]],
    spec: QuerySpec | None,
    result: TableResult | None,
    excel: Callable[[list[tuple[str, str]]], str] | None = None,
) -> dict[str, Any] | None:
    """Precompute the URLs/state the table fragment needs (search, chips, sort
    headers, pager, exports). Templates never assemble query strings.

    ``url(ext, pairs)`` builds a link to the same table (a report, or a saved
    run) in ``ext`` with ``pairs`` as its query string. ``excel(pairs)``, for a
    report, is the URL of its "Use in Excel" box for the current view.

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
        chips.append({"label": label, "url": url("html", _drop_first(keep, (f.key, f.value)))})
    for fragment in spec.q:
        chips.append(
            {
                "label": f"search: {fragment}",
                "url": url("html", _drop_first(keep, ("q", fragment))),
            }
        )

    ui: dict[str, Any] = {
        "search_action": url("html", []),
        "search_hidden": keep,
        "chips": chips,
        "exports": [(fmt, url(fmt, keep)) for fmt in ("parquet", "csv", "json", "xlsx")],
        "excel": excel(_excel_view(keep)) if excel is not None else None,
        "sort_state": {},
        "sort_urls": {},
        "pager": {"prev_url": None, "next_url": None},
        "back_to_live": (
            url("html", [(k, v) for k, v in keep if k != "version"]) if spec.version else None
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
        ui["sort_urls"][col] = url("html", base_no_sort + [("sort", token)])

    if result.page > 1:
        prev = keep if result.page == 2 else keep + [("page", str(result.page - 1))]
        ui["pager"]["prev_url"] = url("html", prev)
    if result.page < result.page_count:
        ui["pager"]["next_url"] = url("html", keep + [("page", str(result.page + 1))])
    return ui
