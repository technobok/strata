"""strata-worker: runs queued datamodule builds, and queues scheduled ones.

One process runs ``--concurrency`` build threads over its ``--queue``\\ s. Each
thread claims the oldest due job (one running job per datamodule, enforced in
SQL) and builds it under the datamodule's lease. The main thread heartbeats
this worker's ``worker_process`` row and, every ``worker.poll_interval``
seconds, sweeps up after dead processes: running jobs whose worker vanished,
and 'building' versions whose lease expired. Several worker processes may run
side by side (claims are atomic).

SIGTERM/SIGINT stop claiming; running builds finish first (docker's stop
timeout may kill them sooner, in which case their leases expire and the next
sweep fails them).
"""

import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import click
from flask import Flask

from strata.config import live_int

log = logging.getLogger("strata.worker")

JOB_POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 10.0


class Worker:
    def __init__(self, app: Flask, queues: list[str], concurrency: int) -> None:
        self.app = app
        self.queues = queues
        self.concurrency = max(1, concurrency)
        self.hostname = socket.gethostname()
        self.id = f"{self.hostname}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop = threading.Event()

    # -- lifecycle -------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        from strata.models.worker_process import WorkerProcess

        with self.app.app_context():
            WorkerProcess.register(
                self.id, self.hostname, os.getpid(), self.queues, self.concurrency
            )
        log.info(
            "worker %s started (queues=%s, concurrency=%d)",
            self.id,
            ",".join(self.queues),
            self.concurrency,
        )
        try:
            if once:
                self.sweep()
                self.tick_schedules()
                self.drain()
                return
            threads = [
                threading.Thread(target=self._build_loop, name=f"build-{i}", daemon=True)
                for i in range(self.concurrency)
            ]
            for t in threads:
                t.start()
            last_sweep: float | None = None
            while not self.stop.is_set():
                with self.app.app_context():
                    WorkerProcess.heartbeat(self.id)
                    # Read each beat (a live setting): a shorter interval applies at
                    # the next beat, not after the old interval runs out.
                    sweep_every = live_int("worker.poll_interval")
                self.tick_schedules()
                if last_sweep is None or time.monotonic() - last_sweep >= sweep_every:
                    self.sweep()
                    last_sweep = time.monotonic()
                self.stop.wait(HEARTBEAT_SECONDS)
            log.info("worker %s stopping; waiting for running builds", self.id)
            for t in threads:
                t.join()
        finally:
            with self.app.app_context():
                WorkerProcess.unregister(self.id)
            log.info("worker %s stopped", self.id)

    def _build_loop(self) -> None:
        while not self.stop.is_set():
            if not self.run_one():
                self.stop.wait(JOB_POLL_SECONDS)

    # -- units of work -----------------------------------------------------------

    def run_one(self) -> bool:
        """Claim and run one job; False when nothing is due."""
        from strata.models.build_job import BuildJob
        from strata.services import build_queue

        with self.app.app_context():
            job = BuildJob.claim_next(self.queues, self.id)
            if job is None:
                return False
            log.info("job %s: building datamodule %s (%s)", job.id, job.datamodule_id, job.reason)
            build_queue.run_claimed(job)
            return True

    def drain(self) -> int:
        """Run jobs until none is due (``--once``); returns how many ran."""
        ran = 0
        while self.run_one():
            ran += 1
        return ran

    def tick_schedules(self) -> None:
        """Queue builds for due schedules (every heartbeat, so within ~10 s of due)."""
        from strata.services import schedule_service

        try:
            with self.app.app_context():
                fired = schedule_service.run_due()
        except Exception:
            log.exception("scheduler tick failed")
            return
        for line in fired:
            log.info("schedule fired: %s", line)

    def sweep(self) -> None:
        """Clean up after processes that died mid-build."""
        from strata.models.build_job import BuildJob
        from strata.models.worker_process import STALE_SECONDS, WorkerProcess
        from strata.services import datamodule_service, run_service

        with self.app.app_context():
            alive = [w.id for w in WorkerProcess.alive()]
            cutoff = (datetime.now(UTC) - timedelta(seconds=STALE_SECONDS)).isoformat()
            orphans = BuildJob.reap_orphans(alive, cutoff)
            dead = datamodule_service.reap_dead_builds()
            WorkerProcess.forget_stale()
            expired_runs = run_service.prune_runs()
        if orphans or dead:
            log.warning("swept %d orphaned job(s) and %d dead build(s)", orphans, len(dead))
        if expired_runs:
            log.info("removed %d expired run(s)", len(expired_runs))


@click.command()
@click.option(
    "--queue",
    "queues",
    multiple=True,
    default=("default",),
    show_default=True,
    help="Queue to take jobs from (repeat for several).",
)
@click.option(
    "--concurrency",
    type=int,
    default=None,
    help="Build threads (default: the worker.concurrency setting).",
)
@click.option("--once", is_flag=True, help="Run every due job, then exit.")
def main(queues: tuple[str, ...], concurrency: int | None, once: bool) -> None:
    """Run queued datamodule builds."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from strata import create_app

    app = create_app()
    worker = Worker(
        app,
        list(queues),
        concurrency if concurrency is not None else int(app.config.get("WORKER_CONCURRENCY", 2)),
    )

    def _stop(signum: int, _frame: object) -> None:
        log.info("signal %d received", signum)
        worker.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker.run(once=once)


if __name__ == "__main__":
    main()
