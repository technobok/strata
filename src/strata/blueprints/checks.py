"""The exceptions overview (/checks): every check and whether it is flagged."""

from flask import Blueprint, render_template, request

from strata.blueprints.auth import login_required
from strata.models.report_check import SEVERITIES
from strata.services import access, checks_service

bp = Blueprint("checks", __name__)


@bp.route("/checks")
@login_required
def index() -> str:
    severity = request.args.get("severity", "")
    if severity not in SEVERITIES:
        severity = ""
    flagged = request.args.get("flagged") == "1"
    rows = [
        row
        for row in checks_service.overview(severity or None, flagged)
        if access.can_view(row.datamodule)
    ]
    return render_template(
        "checks/index.html", rows=rows, severities=SEVERITIES, severity=severity, flagged=flagged
    )
