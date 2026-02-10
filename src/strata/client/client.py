"""StrataClient facade — unified API for both local and HTTP modes."""

from typing import Any

from strata.client.models import ReportResult, ReportSummary


class StrataClient:
    """Main client for the Strata reporting system.

    Supports two modes:
    - Local mode: direct SQLite + DuckDB access (same machine)
    - HTTP mode: remote API calls (requires httpx)

    Usage:
        # Local mode (same machine, direct DB access)
        client = StrataClient(db_path="/path/to/strata.sqlite3")

        # HTTP mode (remote server)
        client = StrataClient(server_url="https://strata.example.com", api_key="sk_...")

        # Run a report
        result = client.run_report("report-uuid", {"date_from": "2026-01-01"})

        # Get a previous run
        result = client.get_run("run-uuid")

        # List reports
        reports = client.list_reports()
    """

    def __init__(
        self,
        db_path: str | None = None,
        server_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if db_path:
            from strata.client.backends.local import LocalBackend

            self.backend = LocalBackend(db_path)
            self.mode = "local"
        elif server_url and api_key:
            from strata.client.backends.http import HttpBackend

            self.backend = HttpBackend(server_url, api_key)
            self.mode = "http"
        else:
            raise ValueError(
                "Provide either db_path (local mode) or server_url + api_key (HTTP mode)"
            )

    def run_report(
        self,
        report_uuid: str,
        parameters: dict[str, Any] | None = None,
    ) -> ReportResult:
        """Run a report and return the result."""
        return self.backend.run_report(report_uuid, parameters)

    def get_run(self, run_uuid: str) -> ReportResult | None:
        """Get a previous run's result."""
        return self.backend.get_run(run_uuid)

    def list_reports(self) -> list[ReportSummary]:
        """List available reports."""
        return self.backend.list_reports()
