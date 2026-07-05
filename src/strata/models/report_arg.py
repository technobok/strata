"""Report argument model — a typed parameter of a report macro.

Args become the macro's parameters (in ``display_order``); values from the
request are cast by ``data_type`` and bound when the report is served. Phase 1
supports single-value ``input`` widgets; ``select``/``multiselect`` and
``constraint_query``-populated options arrive in Phase 3.
"""

from dataclasses import dataclass
from typing import Any

from strata.db import get_db, transaction

DATA_TYPES = ("text", "integer", "number", "decimal", "date", "time", "boolean")


@dataclass
class ReportArg:
    id: int
    report_id: int
    name: str
    data_type: str
    widget: str
    default_value: str | None
    required: bool
    display_order: int
    description: str
    constraint_query: str | None
    constraint_json: str | None

    _COLUMNS = (
        "id, report_id, name, data_type, widget, default_value, required, "
        "display_order, description, constraint_query, constraint_json"
    )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ReportArg:
        return ReportArg(
            id=int(row[0]),
            report_id=int(row[1]),
            name=str(row[2]),
            data_type=str(row[3]),
            widget=str(row[4]),
            default_value=str(row[5]) if row[5] is not None else None,
            required=bool(row[6]),
            display_order=int(row[7]),
            description=str(row[8]),
            constraint_query=str(row[9]) if row[9] is not None else None,
            constraint_json=str(row[10]) if row[10] is not None else None,
        )

    @staticmethod
    def get_for_report(report_id: int) -> list[ReportArg]:
        db = get_db()
        rows = db.execute(
            f"SELECT {ReportArg._COLUMNS} FROM report_arg WHERE report_id = ? "
            "ORDER BY display_order, id",
            (report_id,),
        ).fetchall()
        return [ReportArg._from_row(row) for row in rows]

    @staticmethod
    def replace_all(report_id: int, args: list[dict[str, Any]]) -> None:
        """Delete this report's args and reinsert the given list, in order.

        Each dict carries: name, data_type, default_value, required, description.
        """
        with transaction() as cursor:
            cursor.execute("DELETE FROM report_arg WHERE report_id = ?", (report_id,))
            for order, arg in enumerate(args):
                cursor.execute(
                    "INSERT INTO report_arg (report_id, name, data_type, widget, default_value, "
                    "required, display_order, description) VALUES (?, ?, ?, 'input', ?, ?, ?, ?)",
                    (
                        report_id,
                        str(arg["name"]).strip(),
                        str(arg["data_type"]),
                        arg.get("default_value") or None,
                        1 if arg.get("required") else 0,
                        order,
                        str(arg.get("description", "")).strip(),
                    ),
                )
