"""API link model for GUID-based report access."""

import json
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction


@dataclass
class ApiLink:
    id: int
    uuid: str
    report_id: int
    name: str
    fixed_parameters_json: str | None
    parameterised_params_json: str | None
    created_by: str
    expires_at: str | None
    enabled: bool
    last_used_at: str | None
    use_count: int
    created_at: str

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> ApiLink:
        return ApiLink(
            id=int(row[0]),
            uuid=str(row[1]),
            report_id=int(row[2]),
            name=str(row[3]),
            fixed_parameters_json=str(row[4]) if row[4] is not None else None,
            parameterised_params_json=str(row[5]) if row[5] is not None else None,
            created_by=str(row[6]),
            expires_at=str(row[7]) if row[7] is not None else None,
            enabled=bool(row[8]),
            last_used_at=str(row[9]) if row[9] is not None else None,
            use_count=int(row[10]),
            created_at=str(row[11]),
        )

    _COLUMNS = (
        "id, uuid, report_id, name, fixed_parameters_json, "
        "parameterised_params_json, created_by, expires_at, enabled, "
        "last_used_at, use_count, created_at"
    )

    @staticmethod
    def get_by_uuid(link_uuid: str) -> ApiLink | None:
        db = get_db()
        row = db.execute(
            f"SELECT {ApiLink._COLUMNS} FROM api_link WHERE uuid = ?",
            (link_uuid,),
        ).fetchone()
        return ApiLink._from_row(row) if row else None

    @staticmethod
    def get_for_report(report_id: int) -> list[ApiLink]:
        db = get_db()
        rows = db.execute(
            f"SELECT {ApiLink._COLUMNS} FROM api_link WHERE report_id = ? ORDER BY created_at DESC",
            (report_id,),
        ).fetchall()
        return [ApiLink._from_row(row) for row in rows]

    @staticmethod
    def create(
        report_id: int,
        name: str,
        created_by: str,
        fixed_parameters: dict[str, Any] | None = None,
        parameterised_params: list[str] | None = None,
        expires_at: str | None = None,
    ) -> ApiLink:
        now = datetime.now(UTC).isoformat()
        link_uuid = str(uuid_lib.uuid4())
        fixed_json = json.dumps(fixed_parameters) if fixed_parameters else None
        param_json = json.dumps(parameterised_params) if parameterised_params else None

        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO api_link "
                "(uuid, report_id, name, fixed_parameters_json, "
                "parameterised_params_json, created_by, expires_at, "
                "enabled, use_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)",
                (
                    link_uuid,
                    report_id,
                    name.strip(),
                    fixed_json,
                    param_json,
                    created_by,
                    expires_at,
                    now,
                ),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            link_id = int(row[0]) if row else 0

        return ApiLink(
            id=link_id,
            uuid=link_uuid,
            report_id=report_id,
            name=name.strip(),
            fixed_parameters_json=fixed_json,
            parameterised_params_json=param_json,
            created_by=created_by,
            expires_at=expires_at,
            enabled=True,
            last_used_at=None,
            use_count=0,
            created_at=now,
        )

    def record_use(self) -> None:
        now = datetime.now(UTC).isoformat()
        with transaction() as cursor:
            cursor.execute(
                "UPDATE api_link SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                (now, self.id),
            )
        self.use_count += 1
        self.last_used_at = now

    def rotate_uuid(self) -> str:
        new_uuid = str(uuid_lib.uuid4())
        with transaction() as cursor:
            cursor.execute(
                "UPDATE api_link SET uuid = ? WHERE id = ?",
                (new_uuid, self.id),
            )
        self.uuid = new_uuid
        return new_uuid

    def is_valid(self) -> bool:
        if not self.enabled:
            return False
        if self.expires_at:
            now = datetime.now(UTC).isoformat()
            if now > self.expires_at:
                return False
        return True

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM api_link WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]

    def get_fixed_parameters(self) -> dict[str, Any]:
        if not self.fixed_parameters_json:
            return {}
        return json.loads(self.fixed_parameters_json)

    def get_parameterised_params(self) -> list[str]:
        if not self.parameterised_params_json:
            return []
        return json.loads(self.parameterised_params_json)
