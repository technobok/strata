"""Access control service for reports."""

from strata.db import get_db
from strata.models.report import Report


def can_run(username: str, report_id: int) -> bool:
    """Check if a user can run a report.

    Rules:
    - Reports with no ACL entries are open to all
    - Report creator always has access
    - User ACL with 'run' or 'edit' permission grants access
    """
    report = Report.get_by_id(report_id)
    if not report:
        return False

    # Creator always has access
    if report.created_by == username:
        return True

    db = get_db()

    # Check if report has any ACL entries
    row = db.execute(
        "SELECT COUNT(*) FROM report_access WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    acl_count = int(row[0]) if row else 0

    # No ACL entries means open to all
    if acl_count == 0:
        return True

    # Check user-level access (run or edit)
    row = db.execute(
        "SELECT COUNT(*) FROM report_access "
        "WHERE report_id = ? AND access_type = 'user' AND access_target = ? "
        "AND permission IN ('run', 'edit')",
        (report_id, username),
    ).fetchone()
    if row and int(row[0]) > 0:
        return True

    return False


def can_edit(username: str, report_id: int) -> bool:
    """Check if a user can edit a report.

    Rules:
    - Report creator always has edit access
    - User ACL with 'edit' permission grants access
    - If no ACL entries, all users can edit
    """
    report = Report.get_by_id(report_id)
    if not report:
        return False

    if report.created_by == username:
        return True

    db = get_db()

    row = db.execute(
        "SELECT COUNT(*) FROM report_access WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    acl_count = int(row[0]) if row else 0

    if acl_count == 0:
        return True

    row = db.execute(
        "SELECT COUNT(*) FROM report_access "
        "WHERE report_id = ? AND access_type = 'user' AND access_target = ? "
        "AND permission = 'edit'",
        (report_id, username),
    ).fetchone()
    if row and int(row[0]) > 0:
        return True

    return False


def accessible_report_ids(username: str, permission: str = "run") -> list[int] | None:
    """Get IDs of reports accessible to a user, or None if all are accessible.

    Returns None when all reports are accessible (optimization to avoid filtering).
    """
    db = get_db()

    # Get all report IDs that have ACL entries
    restricted_rows = db.execute("SELECT DISTINCT report_id FROM report_access").fetchall()

    if not restricted_rows:
        return None  # No restrictions at all

    restricted_ids = {int(r[0]) for r in restricted_rows}

    # Get all report IDs
    all_rows = db.execute("SELECT id, created_by FROM report").fetchall()

    accessible = []
    for row in all_rows:
        rid = int(row[0])
        created_by = str(row[1])

        if rid not in restricted_ids:
            # No ACL = open to all
            accessible.append(rid)
        elif created_by == username:
            # Creator always has access
            accessible.append(rid)
        else:
            # Check ACL
            if permission == "run":
                perm_check = "AND permission IN ('run', 'edit')"
            else:
                perm_check = "AND permission = 'edit'"

            acl_row = db.execute(
                f"SELECT COUNT(*) FROM report_access "
                f"WHERE report_id = ? AND access_type = 'user' AND access_target = ? {perm_check}",
                (rid, username),
            ).fetchone()
            if acl_row and int(acl_row[0]) > 0:
                accessible.append(rid)

    return accessible
