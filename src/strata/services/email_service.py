"""Email service for sending report results via Outbox."""

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def send_report_email(
    recipients: list[str],
    report_name: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    xlsx_bytes: bytes,
    max_inline_rows: int = 100,
) -> bool:
    """Send report results via email using Outbox client.

    If row_count <= max_inline_rows, renders an HTML table inline.
    Always attaches the XLSX file.
    """
    outbox_db = os.environ.get("OUTBOX_DB")
    if not outbox_db:
        log.warning("OUTBOX_DB not set, skipping email send")
        return False

    try:
        from outbox.client import Attachment, Message, OutboxClient

        client = OutboxClient(db_path=outbox_db)
    except Exception as e:
        log.error("Failed to create Outbox client: %s", e)
        return False

    row_count = len(rows)
    subject = f"Strata Report: {report_name} ({row_count} rows)"

    # Build HTML body
    body_parts = [f"<h2>{report_name}</h2>"]
    body_parts.append(f"<p>{row_count} row{'s' if row_count != 1 else ''} returned.</p>")

    if row_count > 0 and row_count <= max_inline_rows:
        body_parts.append(
            "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;'>"
        )
        body_parts.append("<thead><tr>")
        for col in columns:
            body_parts.append(f"<th style='background:#f0f0f0;padding:4px 8px;'>{col}</th>")
        body_parts.append("</tr></thead><tbody>")
        for row in rows:
            body_parts.append("<tr>")
            for cell in row:
                body_parts.append(
                    f"<td style='padding:4px 8px;'>{cell if cell is not None else ''}</td>"
                )
            body_parts.append("</tr>")
        body_parts.append("</tbody></table>")
    elif row_count > max_inline_rows:
        body_parts.append(f"<p>Results exceed {max_inline_rows} rows. See attached XLSX file.</p>")

    body_parts.append("<p><small>Sent by Strata Reporting System</small></p>")
    body_html = "\n".join(body_parts)

    filename = f"{report_name.replace(' ', '_')}.xlsx"
    attachment = Attachment(
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=xlsx_bytes,
    )

    mail_sender = os.environ.get("MAIL_SENDER", "strata@localhost")

    try:
        message = Message(
            from_address=mail_sender,
            to=recipients,
            subject=subject,
            body=body_html,
            body_type="html",
            source_app="strata",
            attachments=[attachment],
        )
        result = client.submit_message(message)
        log.info("Email queued: %s to %s", result.uuid, recipients)
        return True
    except Exception as e:
        log.error("Failed to queue email: %s", e)
        return False
