"""Standalone dataclasses for the Strata client library."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParameterInfo:
    name: str
    param_type: str
    data_type: str
    default_value: str | None = None
    description: str = ""
    required: bool = True


@dataclass
class ReportSummary:
    uuid: str
    name: str
    description: str
    created_by: str
    modified_at: str


@dataclass
class ReportResult:
    run_uuid: str
    status: str
    report_name: str = ""
    row_count: int = 0
    duration_ms: int = 0
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
