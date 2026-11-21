"""Diagnostics.

``/diag/auth-probe.json`` reports what a request carried when it reached Strata
(``services/auth_probe``), to find out how Excel gets past the Entra proxy in
front of it. It needs no login, because Excel must reach it, and answers only
while the ``diag.auth_probe`` setting is on (404 otherwise). Every answer also
goes to the app log.
"""

import json
import logging

from flask import Blueprint, abort, jsonify, request
from flask.wrappers import Response

from strata.config import live_bool
from strata.services import auth_probe

log = logging.getLogger(__name__)

bp = Blueprint("diag", __name__, url_prefix="/diag")


@bp.route("/auth-probe.json")
def auth_probe_json() -> Response:
    if not live_bool("diag.auth_probe"):
        abort(404)
    tenant = request.args.get("challenge")
    if tenant is not None and not auth_probe.TENANT.fullmatch(tenant):
        resp = jsonify(
            error="challenge must be a tenant id or domain: letters, digits, '.' and '-', "
            "at most 64 characters"
        )
        resp.status_code = 400
        resp.headers["Cache-Control"] = "no-store"
        return resp
    report = auth_probe.describe(request, tenant)
    log.info("auth probe: %s", json.dumps(report, sort_keys=True))
    resp = jsonify(report)
    resp.headers["Cache-Control"] = "no-store"
    if tenant is not None and report["challenged"]:
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = auth_probe.challenge_header(tenant)
    return resp
