"""Tag model for labeling reports."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from strata.db import get_db, transaction

# 32 color palette from reds through pinks
TAG_COLORS = [
    "#dc2626",
    "#ef4444",
    "#f87171",
    "#fca5a5",  # reds
    "#ea580c",
    "#f97316",
    "#fb923c",
    "#fdba74",  # oranges
    "#d97706",
    "#f59e0b",
    "#fbbf24",
    "#fcd34d",  # ambers
    "#16a34a",
    "#22c55e",
    "#4ade80",
    "#86efac",  # greens
    "#0d9488",
    "#14b8a6",
    "#2dd4bf",
    "#5eead4",  # teals
    "#2563eb",
    "#3b82f6",
    "#60a5fa",
    "#93c5fd",  # blues
    "#7c3aed",
    "#8b5cf6",
    "#a78bfa",
    "#c4b5fd",  # purples
    "#db2777",
    "#ec4899",
    "#f472b6",
    "#f9a8d4",  # pinks
]

# Light colors that need dark text
LIGHT_TAG_COLORS = {
    "#fca5a5",
    "#fdba74",
    "#fcd34d",
    "#86efac",
    "#5eead4",
    "#93c5fd",
    "#c4b5fd",
    "#f9a8d4",
}

DEFAULT_TAG_COLOR = "#3b82f6"


def is_light_color(color: str) -> bool:
    """Check if a color needs dark text for contrast."""
    return color.lower() in LIGHT_TAG_COLORS


@dataclass
class Tag:
    id: int
    name: str
    color: str
    created_at: str

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Tag:
        return Tag(
            id=int(row[0]),
            name=str(row[1]),
            color=str(row[2]),
            created_at=str(row[3]),
        )

    @staticmethod
    def get_by_id(tag_id: int) -> Tag | None:
        db = get_db()
        row = db.execute(
            "SELECT id, name, color, created_at FROM tag WHERE id = ?",
            (tag_id,),
        ).fetchone()
        return Tag._from_row(row) if row else None

    @staticmethod
    def get_by_name(name: str) -> Tag | None:
        db = get_db()
        row = db.execute(
            "SELECT id, name, color, created_at FROM tag WHERE LOWER(name) = LOWER(?)",
            (name,),
        ).fetchone()
        return Tag._from_row(row) if row else None

    @staticmethod
    def get_all() -> list[Tag]:
        db = get_db()
        rows = db.execute(
            "SELECT id, name, color, created_at FROM tag ORDER BY LOWER(name)"
        ).fetchall()
        return [Tag._from_row(row) for row in rows]

    @staticmethod
    def search(query: str, limit: int = 20) -> list[Tag]:
        db = get_db()
        rows = db.execute(
            "SELECT id, name, color, created_at FROM tag "
            "WHERE LOWER(name) LIKE LOWER(?) || '%' "
            "ORDER BY LOWER(name) LIMIT ?",
            (query, limit),
        ).fetchall()
        return [Tag._from_row(row) for row in rows]

    @staticmethod
    def create(name: str, color: str | None = None) -> Tag:
        now = datetime.now(UTC).isoformat()
        if color is None:
            color = random.choice(TAG_COLORS)
        elif color not in TAG_COLORS:
            color = DEFAULT_TAG_COLOR

        with transaction() as cursor:
            cursor.execute(
                "INSERT INTO tag (name, color, created_at) VALUES (?, ?, ?)",
                (name.strip(), color, now),
            )
            row = cursor.execute("SELECT last_insert_rowid()").fetchone()
            tag_id = int(row[0]) if row else 0

        return Tag(id=tag_id, name=name.strip(), color=color, created_at=now)

    @staticmethod
    def get_or_create(name: str) -> Tag:
        existing = Tag.get_by_name(name)
        if existing:
            return existing
        return Tag.create(name)

    def update(self, name: str | None = None, color: str | None = None) -> bool:
        updates: list[str] = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name.strip())
        if color is not None and color in TAG_COLORS:
            updates.append("color = ?")
            params.append(color)

        if not updates:
            return False

        params.append(self.id)
        with transaction() as cursor:
            cursor.execute(f"UPDATE tag SET {', '.join(updates)} WHERE id = ?", params)

        if name is not None:
            self.name = name.strip()
        if color is not None and color in TAG_COLORS:
            self.color = color
        return True

    def delete(self) -> bool:
        with transaction() as cursor:
            cursor.execute("DELETE FROM tag WHERE id = ?", (self.id,))
            return cursor.execute("SELECT changes()").fetchone()[0] > 0  # type: ignore[index]

    def usage_count(self) -> int:
        db = get_db()
        row = db.execute("SELECT COUNT(*) FROM report_tag WHERE tag_id = ?", (self.id,)).fetchone()
        return int(row[0]) if row else 0

    def is_light(self) -> bool:
        return is_light_color(self.color)
