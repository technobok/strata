"""Export service for generating XLSX files from query results."""

import io
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


def generate_xlsx(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    sheet_name: str = "Results",
) -> bytes:
    """Generate an XLSX file from query results.

    Returns the file contents as bytes.
    """
    wb = Workbook()
    ws = wb.active
    if ws is None:
        ws = wb.create_sheet()
    ws.title = sheet_name

    # Write headers
    for col_idx, col_name in enumerate(columns, 1):
        ws.cell(row=1, column=col_idx, value=col_name)

    # Write data
    for row_idx, row in enumerate(rows, 2):
        for col_idx, value in enumerate(row, 1):
            ws.cell(row=row_idx, column=col_idx, value=value)

    # Create Excel table if there is data
    if rows and columns:
        last_col = get_column_letter(len(columns))
        last_row = len(rows) + 1
        table_ref = f"A1:{last_col}{last_row}"

        table = Table(displayName="Results", ref=table_ref)
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        table.tableStyleInfo = style
        ws.add_table(table)

    # Auto-fit column widths (approximate)
    for col_idx, col_name in enumerate(columns, 1):
        max_len = len(str(col_name))
        for row in rows[:100]:  # Sample first 100 rows
            val = row[col_idx - 1]
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 50)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def generate_xlsx_from_cache(result_hash: str, sheet_name: str = "Results") -> bytes | None:
    """Generate XLSX from a cached Parquet result."""
    from strata.services.cache_service import read_result

    columns, rows, _ = read_result(result_hash)
    if not columns:
        return None

    return generate_xlsx(columns, rows, sheet_name)
