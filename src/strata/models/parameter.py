"""Parameter model for report parameter definitions."""

from dataclasses import dataclass
from typing import Any

from strata.db import get_db, transaction

VALID_PARAM_TYPES = ("structural", "value")
VALID_DATA_TYPES = ("string", "integer", "float", "decimal", "date", "boolean")


@dataclass
class Parameter:
    id: int
    report_id: int
    name: str
    param_type: str
    data_type: str
    default_value: str | None
    description: str
    display_order: int
    required: bool

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Parameter:
        return Parameter(
            id=int(row[0]),
            report_id=int(row[1]),
            name=str(row[2]),
            param_type=str(row[3]),
            data_type=str(row[4]),
            default_value=str(row[5]) if row[5] is not None else None,
            description=str(row[6]),
            display_order=int(row[7]),
            required=bool(row[8]),
        )

    _COLUMNS = (
        "id, report_id, name, param_type, data_type, "
        "default_value, description, display_order, required"
    )

    @staticmethod
    def get_for_report(report_id: int) -> list[Parameter]:
        db = get_db()
        rows = db.execute(
            f"SELECT {Parameter._COLUMNS} FROM report_parameter "
            "WHERE report_id = ? ORDER BY display_order, name",
            (report_id,),
        ).fetchall()
        return [Parameter._from_row(row) for row in rows]

    @staticmethod
    def get_by_id(param_id: int) -> Parameter | None:
        db = get_db()
        row = db.execute(
            f"SELECT {Parameter._COLUMNS} FROM report_parameter WHERE id = ?",
            (param_id,),
        ).fetchone()
        return Parameter._from_row(row) if row else None

    @staticmethod
    def create(
        report_id: int,
        name: str,
        param_type: str,
        data_type: str,
        default_value: str | None = None,
        description: str = "",
        display_order: int = 0,
        required: bool = True,
    ) -> Parameter:
        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO report_parameter "
                "(report_id, name, param_type, data_type, default_value, "
                "description, display_order, required) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    name,
                    param_type,
                    data_type,
                    default_value,
                    description,
                    display_order,
                    int(required),
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            param_id = int(row[0]) if row else 0

        return Parameter(
            id=param_id,
            report_id=report_id,
            name=name,
            param_type=param_type,
            data_type=data_type,
            default_value=default_value,
            description=description,
            display_order=display_order,
            required=required,
        )

    def update(
        self,
        data_type: str | None = None,
        default_value: str | None = ...,  # type: ignore[assignment]
        description: str | None = None,
        display_order: int | None = None,
        required: bool | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []

        if data_type is not None:
            updates.append("data_type = ?")
            params.append(data_type)
        if default_value is not ...:
            updates.append("default_value = ?")
            params.append(default_value)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if display_order is not None:
            updates.append("display_order = ?")
            params.append(display_order)
        if required is not None:
            updates.append("required = ?")
            params.append(int(required))

        if not updates:
            return False

        params.append(self.id)

        with transaction() as cursor:
            cursor.execute(
                f"UPDATE report_parameter SET {', '.join(updates)} WHERE id = ?",
                params,
            )

        if data_type is not None:
            self.data_type = data_type
        if default_value is not ...:
            self.default_value = default_value  # type: ignore[assignment]
        if description is not None:
            self.description = description
        if display_order is not None:
            self.display_order = display_order
        if required is not None:
            self.required = required
        return True

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM report_parameter WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]

    @staticmethod
    def sync_parameters(
        report_id: int,
        extracted: list[dict[str, str]],
    ) -> tuple[list[str], list[str]]:
        """Sync parameters with extracted names from SQL template.

        Returns (added, removed) name lists.
        extracted is a list of dicts with 'name' and 'param_type' keys.
        """
        existing = Parameter.get_for_report(report_id)
        existing_names = {p.name for p in existing}
        extracted_names = {e["name"] for e in extracted}

        added = []
        for item in extracted:
            if item["name"] not in existing_names:
                Parameter.create(
                    report_id=report_id,
                    name=item["name"],
                    param_type=item["param_type"],
                    data_type="string",
                )
                added.append(item["name"])

        removed = []
        for p in existing:
            if p.name not in extracted_names:
                removed.append(p.name)

        return added, removed
