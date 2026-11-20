"""Requesting builds, and running one build job (a worker's unit of work).

``request_build`` records a coalesced ``build_job`` row; a ``strata-worker``
process claims and runs it. In inline mode (``WORKER_INLINE``: tests and
``strata-web --dev``) the caller runs the job itself, synchronously, through
the same code path.
"""

import logging
import os
import socket

from flask import current_app

from strata.models.build_job import BuildJob
from strata.models.datamodule import Datamodule
from strata.services import datamodule_service

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
            version = datamodule_service.ensure_fresh(dm, job.reason, dm.ttl_seconds)
            job.finish("done" if version.id != before else "skipped", version_id=version.id)
    except Exception as exc:
        log.warning("build job %s for %s failed: %s", job.id, dm.path, exc)
        job.finish("failed", error=str(exc))
