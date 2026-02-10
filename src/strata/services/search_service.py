"""Full-text search service using FTS5."""

from strata.db import get_db


def index_report(report_id: int, name: str, description: str, tags_text: str = "") -> None:
    """Add or update a report in the FTS index."""
    db = get_db()
    db.execute("DELETE FROM report_fts WHERE rowid = ?", (report_id,))
    db.execute(
        "INSERT INTO report_fts (rowid, name, description, tags_text) VALUES (?, ?, ?, ?)",
        (report_id, name, description, tags_text),
    )


def remove_from_index(report_id: int) -> None:
    """Remove a report from the FTS index."""
    db = get_db()
    db.execute("DELETE FROM report_fts WHERE rowid = ?", (report_id,))


def rebuild_index() -> int:
    """Drop and rebuild the entire FTS index from the report table."""
    db = get_db()

    db.execute("DELETE FROM report_fts")

    rows = db.execute("SELECT id, name, description FROM report").fetchall()

    count = 0
    for row in rows:
        report_id = int(row[0])
        name = str(row[1]) if row[1] else ""
        description = str(row[2]) if row[2] else ""

        # Gather tags
        tag_rows = db.execute(
            "SELECT t.name FROM tag t "
            "JOIN report_tag rt ON rt.tag_id = t.id "
            "WHERE rt.report_id = ?",
            (report_id,),
        ).fetchall()
        tags_text = " ".join(str(r[0]) for r in tag_rows)

        db.execute(
            "INSERT INTO report_fts (rowid, name, description, tags_text) VALUES (?, ?, ?, ?)",
            (report_id, name, description, tags_text),
        )
        count += 1

    return count


def search(query: str, limit: int = 50, tag: str | None = None) -> list[dict]:
    """Search reports with optional tag filter.

    Returns list of dicts with id, name, description, snippet.
    """
    db = get_db()

    has_query = bool(query and query.strip())

    if has_query:
        safe_query = " ".join(f'"{term}"' for term in query.split())
        sql = (
            "SELECT r.id, r.uuid, r.name, r.description, r.modified_at, "
            "snippet(report_fts, 1, '<mark>', '</mark>', '...', 40) as snippet "
            "FROM report_fts fts "
            "JOIN report r ON r.id = fts.rowid "
        )
        conditions = ["report_fts MATCH ?"]
        params: list[str | int] = [safe_query]
    else:
        sql = (
            "SELECT r.id, r.uuid, r.name, r.description, r.modified_at, "
            "'' as snippet "
            "FROM report r "
        )
        conditions: list[str] = []
        params: list[str | int] = []

    if tag:
        sql += "JOIN report_tag rt ON rt.report_id = r.id JOIN tag t ON t.id = rt.tag_id "
        conditions.append("t.name = ?")
        params.append(tag)

    if conditions:
        sql += "WHERE " + " AND ".join(conditions)

    sql += (" ORDER BY rank" if has_query else " ORDER BY r.modified_at DESC") + " LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()

    results: list[dict] = []
    for row in rows:
        results.append(
            {
                "id": int(row[0]),
                "uuid": str(row[1]),
                "name": str(row[2]),
                "description": str(row[3]),
                "modified_at": str(row[4]),
                "snippet": str(row[5]) if row[5] else "",
            }
        )
    return results
