"""HTTP API backend for StrataClient (remote server)."""

from typing import Any

import httpx

from strata.client.models import ReportResult, ReportSummary


class HttpBackend:
    """Backend that communicates with a remote Strata server via JSON API."""

    def __init__(
        self,
        server_url: str,
        api_key: str,
        timeout: float = 60.0,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.server_url,
            headers={"X-API-Key": self.api_key},
            timeout=self.timeout,
        )

    def run_report(
        self,
        report_uuid: str,
        parameters: dict[str, Any] | None = None,
    ) -> ReportResult:
        """Run a report via the API."""
        payload: dict[str, Any] = {}
        if parameters:
            payload["parameters"] = parameters

        with self._client() as client:
            resp = client.post(f"/api/v1/reports/{report_uuid}/run", json=payload)
            if resp.status_code == 404:
                return ReportResult(
                    run_uuid="",
                    status="error",
                    error=f"Report {report_uuid} not found",
                )
            data = resp.json()
            if resp.status_code >= 400:
                return ReportResult(
                    run_uuid=data.get("run_uuid", ""),
                    status="failed",
                    error=data.get("error", "Unknown error"),
                )
            return ReportResult(
                run_uuid=data["run_uuid"],
                status="completed",
                report_name=data.get("report", ""),
                row_count=data.get("row_count", 0),
                duration_ms=data.get("duration_ms", 0),
                columns=data.get("columns", []),
                rows=data.get("rows", []),
            )

    def get_run(self, run_uuid: str) -> ReportResult | None:
        """Get a run's result from the API."""
        with self._client() as client:
            resp = client.get(f"/api/v1/runs/{run_uuid}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return ReportResult(
                run_uuid=data.get("run_uuid", run_uuid),
                status=data.get("status", ""),
                row_count=data.get("row_count", 0),
                columns=data.get("columns", []),
                rows=data.get("rows", []),
                error=data.get("error"),
            )

    def list_reports(self) -> list[ReportSummary]:
        """List reports from the API."""
        with self._client() as client:
            resp = client.get("/api/v1/reports")
            resp.raise_for_status()
            data = resp.json()
            return [
                ReportSummary(
                    uuid=r["uuid"],
                    name=r["name"],
                    description=r.get("description", ""),
                    created_by=r.get("created_by", ""),
                    modified_at=r.get("modified_at", ""),
                )
                for r in data.get("reports", [])
            ]
