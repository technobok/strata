"""Suggestions for the people fields (a datamodule's owners, viewers and alert list,
a schedule's recipients, a connection's "May use"): Gatekeeper's users and @groups
as you type (``static/js/people.js`` over the vendored Tom Select).

Only for those who edit such a field (writers, admins, a datamodule's owners): the
answer lists people's names and email addresses.
"""

import logging
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from strata.blueprints.auth import forbid, login_required
from strata.services import access

log = logging.getLogger(__name__)

bp = Blueprint("people", __name__, url_prefix="/people")

LIMIT = 20  # suggestions of each kind


@bp.route("/suggest")
@login_required
def suggest() -> Response:
    if not access.edits_people():
        forbid("Only writers, datamodule owners and the Strata admins can look people up.")
    query = request.args.get("q", "").strip()
    gk = current_app.config.get("GATEKEEPER_CLIENT")
    if gk is None or not query:
        return jsonify([])
    found: list[dict[str, Any]] = []
    try:
        if not query.startswith("@"):
            for user in gk.list_users(search=query, enabled_only=True, limit=LIMIT):
                found.append(
                    {
                        "value": user.username,
                        "text": user.fullname or user.username,
                        "detail": user.email or "",
                        "kind": "user",
                    }
                )
        name = query.removeprefix("@").lower()
        groups = [
            grp
            for grp in gk.list_groups()
            if name in grp.name.lower() or name in (grp.description or "").lower()
        ]
        for grp in sorted(groups, key=lambda grp: grp.name.lower())[:LIMIT]:
            found.append(
                {
                    "value": f"@{grp.name}",
                    "text": f"@{grp.name}",
                    "detail": grp.description or "",
                    "kind": "group",
                }
            )
    except Exception:  # noqa: BLE001 -- no suggestions is the answer, not an error page
        log.exception("people suggestions for %r failed", query)
        return jsonify([])
    return jsonify(found)
