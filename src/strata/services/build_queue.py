"""Requesting builds, and running one build job (a worker's unit of work).

``request_build`` records a coalesced ``build_job`` row; a ``strata-worker``
process claims and runs it. In inline mode (``WORKER_INLINE``: tests and
``strata-web --dev``) the caller runs the job itself, synchronously, through
the same code path.
"""

import logging
import os
import socket
import time
from dataclasses import dataclass

from flask import current_app

from strata.models.build_job import BuildJob
from strata.models.datamodule import Datamodule
from strata.models.datamodule_version import DatamoduleVersion
from strata.services import datamodule_service

JOB_POLL_SECONDS = 0.25

log = logging.getLogger(__name__)


def inline_mode() -> bool:
    return bool(current_app.config.get("WORKER_INLINE", False))


def inline_worker_id() -> str:
    # Computed per call: gunicorn forks after preload, so the pid changes.
    return f"inline:{socket.gethostname()}:{os.getpid()}"


def request_build(dm: Datamodule, reason: str, force: bool) -> BuildJob:
    """Queue a build of ``dm`` (coalescing with an active job) and return the job.

    ``force`` builds even if the live version is fresh (manual, schedule,
    upstream); otherwise the job only rebuilds when the datamodule is stale.
    """
    job = BuildJob.enqueue(dm.id, dm.build_queue, reason, force)
    if inline_mode() and job.status == "queued" and job.claim(inline_worker_id()):
        run_claimed(job)
        refreshed = BuildJob.get_by_id(job.id)
        assert refreshed is not None
        job = refreshed
    return job


def run_claimed(job: BuildJob) -> None:
    """Run a job this process has claimed and record the outcome on its row."""
    dm = Datamodule.get_by_id(job.datamodule_id)
    if dm is None:
        job.finish("failed", error="datamodule was deleted")
        return
    try:
        if job.force:
            version = datamodule_service.build_datamodule(dm, job.reason)
            job.finish("done", version_id=version.id)
        else:
            before = dm.live_version_id
            version = datamodule_service.ensure_fresh(
                dm, job.reason, dm.ttl_seconds, run=datamodule_service.BuildRun(auto=True)
            )
            job.finish("done" if version.id != before else "skipped", version_id=version.id)
    except datamodule_service.BackoffError as exc:
        job.finish("skipped", error=str(exc))
    except Exception as exc:
        log.warning("build job %s for %s failed: %s", job.id, dm.path, exc)
        job.finish("failed", error=str(exc))


# ---------------------------------------------------------------------------
# The serve path: which version to serve (it never builds itself)
# ---------------------------------------------------------------------------


@dataclass
class Served:
    """The version a report request should serve, and how fresh it is."""

    version: DatamoduleVersion | None  # None: nothing has been built yet
    stale: bool = False  # older than the TTL: a rebuild is pending or failing
    rebuilding: bool = False  # a rebuild is queued or running
    failure: str | None = None  # why the data could not be refreshed, if it failed


def max_wait_seconds() -> float:
    return float(current_app.config.get("SERVE_MAX_WAIT_SECONDS", 60))


def wait_for(job: BuildJob, seconds: float) -> BuildJob:
    """Poll ``job`` until it is no longer queued/running, or ``seconds`` pass."""
    deadline = time.monotonic() + seconds
    while job.active and time.monotonic() < deadline:
        time.sleep(JOB_POLL_SECONDS)
        refreshed = BuildJob.get_by_id(job.id)
        if refreshed is None:
            break
        job = refreshed
    return job


def servable_version(dm: Datamodule, effective_ttl: int | None) -> Served:
    """Serve fresh data when it can be had within the wait; else the last good data.

    A stale datamodule gets a (coalesced, unforced) rebuild request, and the
    request waits up to ``serve.max_wait_seconds`` for it. If the rebuild has
    not finished by then, or fails, or the datamodule is in failure backoff,
    the previous live version is served, marked stale.
    """
    live = datamodule_service.live_version(dm)
    if live is not None and not datamodule_service.is_stale(dm, live, effective_ttl):
        return Served(live)
    if datamodule_service.in_backoff(dm):
        return Served(live, stale=True, failure=dm.last_build_error)

    job = wait_for(request_build(dm, "stale-read", force=False), max_wait_seconds())
    if job.status in ("done", "skipped") and job.version_id is not None:
        built = DatamoduleVersion.get_by_id(job.version_id)
        if built is not None and built.status in ("ready", "superseded"):
            current = Datamodule.get_by_id(dm.id)
            latest = datamodule_service.live_version(current) if current else None
            return Served(latest or built)
    current = Datamodule.get_by_id(dm.id) or dm
    return Served(
        datamodule_service.live_version(current),
        stale=True,
        rebuilding=job.active,
        failure=job.error if job.status in ("failed", "skipped") else None,
    )
