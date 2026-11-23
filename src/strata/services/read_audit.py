"""The read audit: one ``read_log`` row per read of a datamodule's data.

A route that shows or sends data calls ``note(kind, dm, …)`` as soon as it knows
what is being read — before the checks that may refuse it, so refusals are
recorded too — and ``update(…)`` as it learns more (the key, the version, the rows
shown). One ``after_request`` hook then writes the row with the response's
status, ``X-Row-Count`` and ``Content-Length`` (stream_service sets both exactly),
the time taken, the client address (ProxyFix in production) and the User-Agent.
Flask runs it for a refusal (``forbid``/``abort``), a streamed body (its headers
are already set) and an error. A redirect is not a read and writes nothing.

Writing is best-effort: a failure is logged and never changes the response.
"""

import logging
import time
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from flask import Flask, g, request
from flask.wrappers import Response

from strata.config import live_int
from strata.models.read_log import ReadLog

log = logging.getLogger(__name__)

# What was read: a report page (incl. its HTMX fragments), report data (an export or
# a keyed Excel refresh), a saved run's page or download, a datamodule's Excel key
# shown to someone, a build-script preview (reads parents' rows), a version's
# rendered script, an uploaded source file.
KINDS = ("page", "data", "run", "run-data", "excel-key", "preview", "script", "source")

# The kinds whose response is the data itself: their bytes are recorded (a page's
# size says nothing about what was read).
DOWNLOADS = ("data", "run-data", "source")

QUERY_MAX = 2048
AGENT_MAX = 300


@dataclass
class _Read:
    kind: str
    datamodule_id: int | None = None
    datamodule_path: str | None = None
    report_id: int | None = None
    report_name: str | None = None
    api_key_id: int | None = None
    key_name: str | None = None
    token_prefix: str | None = None
    run_uuid: str | None = None
    version_uuid: str | None = None
    format: str | None = None
    query: str | None = None
    row_count: int | None = None


def note(kind: str, dm: Any = None, report: Any = None, **more: Any) -> None:
    """Start this request's read row. ``dm``/``report`` are models (or None)."""
    if kind not in KINDS:
        raise ValueError(f"unknown read kind {kind!r}")
    read = _Read(kind=kind)
    if dm is not None:
        read.datamodule_id, read.datamodule_path = dm.id, dm.path
    if report is not None:
        read.report_id, read.report_name = report.id, report.name
        if read.datamodule_id is None:
            read.datamodule_id = report.datamodule_id
    g.read_audit = read
    update(**more)


def update(**values: Any) -> None:
    """Add to this request's read row: ``key=`` (an ApiKey), ``version=`` (a
    DatamoduleVersion), ``run=`` (a ReportRun), or any _Read field."""
    read: _Read | None = g.get("read_audit")
    if read is None:
        return
    key = values.pop("key", None)
    if key is not None:
        read.api_key_id, read.key_name, read.token_prefix = key.id, key.name, key.token_prefix
    version = values.pop("version", None)
    if version is not None:
        read.version_uuid = version.uuid
    run = values.pop("run", None)
    if run is not None:
        read.run_uuid = run.uuid
    names = {f.name for f in fields(_Read)}
    for name, value in values.items():
        if name not in names:
            raise TypeError(f"no read field {name!r}")
        setattr(read, name, value)


def _query() -> str | None:
    pairs = [(k, v) for k, v in request.args.items(multi=True) if k != "key"]
    return urlencode(pairs)[:QUERY_MAX] if pairs else None


def _int_header(resp: Response, name: str) -> int | None:
    value = resp.headers.get(name)
    return int(value) if value and value.isdigit() else None


def _write(resp: Response) -> None:
    read: _Read | None = g.pop("read_audit", None)
    if read is None or 300 <= resp.status_code < 400:
        return
    started = g.get("read_audit_started")
    user = g.get("user")
    rows = _int_header(resp, "X-Row-Count")
    ReadLog.record(
        at=g.get("read_audit_at") or datetime.now(UTC).isoformat(),
        kind=read.kind,
        username=user.username if user is not None else None,
        api_key_id=read.api_key_id,
        key_name=read.key_name,
        token_prefix=read.token_prefix,
        datamodule_id=read.datamodule_id,
        datamodule_path=read.datamodule_path,
        report_id=read.report_id,
        report_name=read.report_name,
        run_uuid=read.run_uuid,
        version_uuid=read.version_uuid,
        format=read.format,
        query=read.query if read.query is not None else _query(),
        status=resp.status_code,
        row_count=rows if rows is not None else read.row_count,
        bytes_sent=(
            _int_header(resp, "Content-Length")
            if read.kind in DOWNLOADS and resp.status_code < 400
            else None
        ),
        duration_ms=int((time.perf_counter() - started) * 1000) if started else None,
        remote_addr=request.remote_addr,
        user_agent=(request.headers.get("User-Agent") or "")[:AGENT_MAX] or None,
    )


def init_app(app: Flask) -> None:
    @app.before_request
    def _read_audit_start() -> None:
        g.read_audit_started = time.perf_counter()
        g.read_audit_at = datetime.now(UTC).isoformat()

    @app.after_request
    def _read_audit_write(resp: Response) -> Response:
        try:
            _write(resp)
        except Exception:  # noqa: BLE001 -- the audit never breaks a response
            log.exception("read audit: the row for %s could not be written", request.path)
        return resp


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def prune(dry_run: bool = False, now: datetime | None = None) -> int:
    """Delete rows older than ``audit.retention_days`` (0 keeps them all); returns
    how many went (or would)."""
    days = live_int("audit.retention_days")
    if days <= 0:
        return 0
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
    if dry_run:
        return ReadLog.older_than(cutoff)
    return ReadLog.delete_older_than(cutoff)
