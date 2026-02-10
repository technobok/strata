"""Local backend for StrataClient — direct SQLite + DuckDB access."""

from typing import Any

from strata.client.models import ReportResult, ReportSummary


class LocalBackend:
    """Backend that queries the Strata database directly."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _get_app(self) -> Any:
        """Create a Flask app with the correct database path."""
        import os

        os.environ["STRATA_DB"] = self.db_path
        from strata import create_app

        return create_app()

    def run_report(
        self,
        report_uuid: str,
        parameters: dict[str, Any] | None = None,
    ) -> ReportResult:
        """Run a report and return the result."""
        app = self._get_app()
        with app.app_context():
            from strata.models.parameter import Parameter
            from strata.models.report import Report
            from strata.models.report_run import ReportRun
            from strata.services import cache_service
            from strata.services.query_service import (
                compute_result_hash,
                execute_report,
            )

            report = Report.get_by_uuid(report_uuid)
            if not report:
                return ReportResult(
                    run_uuid="",
                    status="error",
                    error=f"Report {report_uuid} not found",
                )

            params = Parameter.get_for_report(report.id)
            supplied = parameters or {}

            structural_params: dict[str, str] = {}
            value_params: dict[str, str] = {}
            param_types: dict[str, str] = {}

            for param in params:
                value = supplied.get(param.name, "")
                if not value and param.default_value:
                    value = param.default_value

                if param.param_type == "structural":
                    structural_params[param.name] = value
                else:
                    value_params[param.name] = value
                    param_types[param.name] = param.data_type

            run_record = ReportRun.create_running(
                report_id=report.id,
                run_by="client:local",
                parameters=supplied or None,
            )

            result = execute_report(
                sql_template=report.sql_template,
                structural_params=structural_params,
                value_params=value_params,
                param_types=param_types,
            )

            if result.error:
                run_record.mark_failed(result.error, result.duration_ms)
                return ReportResult(
                    run_uuid=run_record.uuid,
                    status="failed",
                    report_name=report.name,
                    error=result.error,
                )

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

            rows_data = [dict(zip(result.columns, row, strict=False)) for row in result.rows]

            return ReportResult(
                run_uuid=run_record.uuid,
                status="completed",
                report_name=report.name,
                row_count=result.row_count,
                duration_ms=result.duration_ms,
                columns=result.columns,
                rows=rows_data,
            )

    def get_run(self, run_uuid: str) -> ReportResult | None:
        """Get a previous run's result from cache."""
        app = self._get_app()
        with app.app_context():
            from strata.models.report import Report
            from strata.models.report_run import ReportRun
            from strata.services import cache_service

            run_record = ReportRun.get_by_uuid(run_uuid)
            if not run_record:
                return None

            report = Report.get_by_id(run_record.report_id)
            report_name = report.name if report else ""

            if run_record.status != "completed" or not run_record.result_hash:
                return ReportResult(
                    run_uuid=run_record.uuid,
                    status=run_record.status,
                    report_name=report_name,
                    error=run_record.error_message,
                )

            columns, rows, total = cache_service.read_result(run_record.result_hash)
            rows_data = [dict(zip(columns, row, strict=False)) for row in rows]

            return ReportResult(
                run_uuid=run_record.uuid,
                status=run_record.status,
                report_name=report_name,
                row_count=total,
                columns=columns,
                rows=rows_data,
            )

    def list_reports(self) -> list[ReportSummary]:
        """List all reports."""
        app = self._get_app()
        with app.app_context():
            from strata.models.report import Report

            reports = Report.get_all()
            return [
                ReportSummary(
                    uuid=r.uuid,
                    name=r.name,
                    description=r.description,
                    created_by=r.created_by,
                    modified_at=r.modified_at,
                )
                for r in reports
            ]
