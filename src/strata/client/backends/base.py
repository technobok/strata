"""Abstract backend protocol for StrataClient."""

from typing import Any, Protocol

from strata.client.models import ReportResult, ReportSummary


class StrataBackend(Protocol):
    """Protocol that all backends must implement."""

    def run_report(
        self,
        report_uuid: str,
        parameters: dict[str, Any] | None = None,
    ) -> ReportResult:
        """Run a report and return the result."""
        ...

    def get_run(self, run_uuid: str) -> ReportResult | None:
        """Get a previous run's result."""
        ...

    def list_reports(self) -> list[ReportSummary]:
        """List available reports."""
        ...
