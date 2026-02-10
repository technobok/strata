"""Background schedule worker - polls for due schedules and runs reports."""

import logging
import signal
import time
from datetime import UTC, datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("strata.worker")

_running = True


def _handle_signal(signum: int, frame: object) -> None:
    global _running
    log.info("Received signal %s, shutting down...", signum)
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def run() -> None:
    """Main worker loop."""
    from strata import create_app

    app = create_app()

    poll_interval = app.config.get("WORKER_POLL_INTERVAL", 30)

    log.info("Schedule worker started (poll=%ds)", poll_interval)

    while _running:
        with app.app_context():
            try:
                _process_due_schedules()
            except Exception:
                log.exception("Error in worker loop")

        # Sleep in small increments so we can respond to signals
        for _ in range(poll_interval * 10):
            if not _running:
                break
            time.sleep(0.1)

    log.info("Worker stopped.")


def _process_due_schedules() -> None:
    """Process all schedules that are due to run."""
    from strata.models.parameter import Parameter
    from strata.models.report import Report
    from strata.models.report_run import ReportRun
    from strata.models.schedule import Schedule
    from strata.services import cache_service
    from strata.services.email_service import send_report_email
    from strata.services.export_service import generate_xlsx
    from strata.services.query_service import compute_result_hash, execute_report
    from strata.services.schedule_service import next_run

    now_iso = datetime.now(UTC).isoformat()
    due_schedules = Schedule.get_due(now_iso)

    if not due_schedules:
        return

    log.info("Processing %d due schedule(s)", len(due_schedules))

    for schedule in due_schedules:
        report = Report.get_by_id(schedule.report_id)
        if not report:
            log.warning(
                "Schedule %s references missing report %d", schedule.uuid, schedule.report_id
            )
            continue

        log.info(
            "Running schedule '%s' (uuid=%s) for report '%s'",
            schedule.name,
            schedule.uuid,
            report.name,
        )

        # Build parameters from schedule's fixed params + report param defaults
        params = Parameter.get_for_report(report.id)
        schedule_params = schedule.get_parameters()

        structural_params: dict[str, str] = {}
        value_params: dict[str, str] = {}
        param_types: dict[str, str] = {}

        for param in params:
            value = schedule_params.get(param.name, "")
            if not value and param.default_value:
                value = param.default_value

            if param.param_type == "structural":
                structural_params[param.name] = value
            else:
                value_params[param.name] = value
                param_types[param.name] = param.data_type

        # Create a run record
        run_record = ReportRun.create_running(
            report_id=report.id,
            run_by=f"schedule:{schedule.name}",
            parameters=schedule_params or None,
        )

        # Execute the report
        result = execute_report(
            sql_template=report.sql_template,
            structural_params=structural_params,
            value_params=value_params,
            param_types=param_types,
        )

        if result.error:
            run_record.mark_failed(result.error, result.duration_ms)
            log.error("Schedule '%s' failed: %s", schedule.name, result.error)
        else:
            # Cache result
            result_hash = compute_result_hash(report.id, result.rendered_sql, value_params)
            cache_service.write_result(result_hash, result.columns, result.types, result.rows)

            column_info = [
                {"name": col, "type": typ}
                for col, typ in zip(result.columns, result.types, strict=False)
            ]
            run_record.mark_completed(
                row_count=result.row_count,
                column_info=column_info,
                result_hash=result_hash,
                duration_ms=result.duration_ms,
            )

            log.info(
                "Schedule '%s' completed: %d rows in %dms",
                schedule.name,
                result.row_count,
                result.duration_ms,
            )

            # Generate XLSX and send email
            xlsx_bytes = generate_xlsx(result.columns, result.rows, report.name[:31])
            recipients = schedule.get_recipients()

            if recipients:
                sent = send_report_email(
                    recipients=recipients,
                    report_name=report.name,
                    columns=result.columns,
                    rows=result.rows,
                    xlsx_bytes=xlsx_bytes,
                    max_inline_rows=schedule.max_inline_rows,
                )
                if sent:
                    log.info("Email sent to %s for schedule '%s'", recipients, schedule.name)
                else:
                    log.warning("Failed to send email for schedule '%s'", schedule.name)

        # Calculate next run time — skip to next future occurrence (no catch-up)
        next_run_dt = next_run(schedule.schedule_definition, datetime.now(UTC))
        next_run_iso = next_run_dt.isoformat() if next_run_dt else None
        schedule.update_after_run(next_run_iso)

        if next_run_iso:
            log.info("Schedule '%s' next run at %s", schedule.name, next_run_iso)
        else:
            log.info("Schedule '%s' has no future runs (disabled)", schedule.name)


if __name__ == "__main__":
    run()
