"""Report-tag junction table operations."""

from strata.db import get_db, transaction
from strata.models.tag import Tag


def get_tags_for_report(report_id: int) -> list[Tag]:
    """Get all tags for a report."""
    db = get_db()
    rows = db.execute(
        "SELECT t.id, t.name, t.color, t.created_at "
        "FROM tag t JOIN report_tag rt ON rt.tag_id = t.id "
        "WHERE rt.report_id = ? ORDER BY LOWER(t.name)",
        (report_id,),
    ).fetchall()
    return [Tag._from_row(row) for row in rows]


def set_tags_for_report(report_id: int, tag_ids: list[int]) -> None:
    """Replace all tags for a report."""
    with transaction() as cursor:
        cursor.execute("DELETE FROM report_tag WHERE report_id = ?", (report_id,))
        for tag_id in tag_ids:
            cursor.execute(
                "INSERT OR IGNORE INTO report_tag (report_id, tag_id) VALUES (?, ?)",
                (report_id, tag_id),
            )


def add_tag_to_report(report_id: int, tag_id: int) -> None:
    """Add a single tag to a report."""
    with transaction() as cursor:
        cursor.execute(
            "INSERT OR IGNORE INTO report_tag (report_id, tag_id) VALUES (?, ?)",
            (report_id, tag_id),
        )


def remove_tag_from_report(report_id: int, tag_id: int) -> None:
    """Remove a single tag from a report."""
    with transaction() as cursor:
        cursor.execute(
            "DELETE FROM report_tag WHERE report_id = ? AND tag_id = ?",
            (report_id, tag_id),
        )


def get_tags_text(report_id: int) -> str:
    """Get space-separated tag names for FTS indexing."""
    tags = get_tags_for_report(report_id)
    return " ".join(t.name for t in tags)
