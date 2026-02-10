"""Tags blueprint — CRUD and search endpoint."""

import json

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from strata.blueprints.auth import login_required
from strata.models.tag import TAG_COLORS, Tag

bp = Blueprint("tags", __name__, url_prefix="/tags")


@bp.route("/")
@login_required
def index() -> str:
    """List all tags."""
    tags = Tag.get_all()
    tag_counts = {tag.id: tag.usage_count() for tag in tags}
    return render_template("tags/index.html", tags=tags, tag_counts=tag_counts)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new() -> str | Response:
    """Create a new tag."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        color = request.form.get("color", "")

        if not name:
            flash("Tag name is required.", "error")
            return render_template("tags/edit.html", tag=None, colors=TAG_COLORS)

        existing = Tag.get_by_name(name)
        if existing:
            flash(f"Tag '{name}' already exists.", "error")
            return render_template("tags/edit.html", tag=None, colors=TAG_COLORS)

        Tag.create(name, color or None)
        flash(f"Tag '{name}' created.", "success")
        return redirect(url_for("tags.index"))

    return render_template("tags/edit.html", tag=None, colors=TAG_COLORS)


@bp.route("/<int:tag_id>/edit", methods=["GET", "POST"])
@login_required
def edit(tag_id: int) -> str | Response:
    """Edit a tag."""
    tag = Tag.get_by_id(tag_id)
    if not tag:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        color = request.form.get("color", "")

        if name:
            tag.update(name=name, color=color or None)
            flash("Tag updated.", "success")
        return redirect(url_for("tags.index"))

    return render_template("tags/edit.html", tag=tag, colors=TAG_COLORS)


@bp.route("/<int:tag_id>/delete", methods=["POST"])
@login_required
def delete(tag_id: int) -> Response:
    """Delete a tag."""
    tag = Tag.get_by_id(tag_id)
    if not tag:
        abort(404)

    tag.delete()
    flash(f"Tag '{tag.name}' deleted.", "success")
    return redirect(url_for("tags.index"))


@bp.route("/search")
@login_required
def search_tags() -> tuple[str, int, dict[str, str]]:
    """JSON endpoint for tom-select tag search."""
    q = request.args.get("q", "")
    tags = Tag.search(q) if q else Tag.get_all()
    results = [{"value": str(tag.id), "text": tag.name, "color": tag.color} for tag in tags]
    return json.dumps(results), 200, {"Content-Type": "application/json"}
