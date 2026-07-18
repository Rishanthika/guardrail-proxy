"""
Mock enterprise tool services.

These stand in for the real systems an autonomous agent would eventually
call (a production database, an SMTP/Graph API email sender, a filesystem
or object store). Each tool:

  - Accepts the same `parameters` shape the agent submitted.
  - Honors `dry_run`: when True, it validates + echoes back a simulated
    result WITHOUT performing the (mock) side effect, matching the
    contract `{"status": "simulated", "dry_run": true, ...}`.
  - Raises `ToolExecutionError` on invalid input so main.py can turn that
    into a clean 400/500 rather than an unhandled exception.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


class ToolExecutionError(Exception):
    pass


class DatabaseTool:
    @staticmethod
    async def delete(*, record_count: int, table: str = "unspecified", dry_run: bool = False) -> dict[str, Any]:
        if not isinstance(record_count, int) or record_count < 0:
            raise ToolExecutionError("record_count must be a non-negative integer")

        if dry_run:
            return {
                "status": "simulated",
                "dry_run": True,
                "tool": "database_delete",
                "would_delete": record_count,
                "table": table,
            }

        # --- mock side effect ---
        return {
            "status": "executed",
            "dry_run": False,
            "tool": "database_delete",
            "deleted": record_count,
            "table": table,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }


class EmailTool:
    @staticmethod
    async def send(
        *,
        email_to: str,
        subject: str = "(no subject)",
        body: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not email_to or "@" not in email_to:
            raise ToolExecutionError("email_to must be a valid email address")

        message_id = str(uuid.uuid4())

        if dry_run:
            return {
                "status": "simulated",
                "dry_run": True,
                "tool": "send_email",
                "would_send_to": email_to,
                "subject": subject,
            }

        # --- mock side effect ---
        return {
            "status": "executed",
            "dry_run": False,
            "tool": "send_email",
            "sent_to": email_to,
            "subject": subject,
            "message_id": message_id,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }


class FileSystemTool:
    @staticmethod
    async def read(*, file_path: str, dry_run: bool = False) -> dict[str, Any]:
        if not file_path:
            raise ToolExecutionError("file_path must be provided")

        if dry_run:
            return {
                "status": "simulated",
                "dry_run": True,
                "tool": "read_file",
                "would_read": file_path,
            }

        # --- mock side effect (no real filesystem access performed) ---
        return {
            "status": "executed",
            "dry_run": False,
            "tool": "read_file",
            "path": file_path,
            "bytes_read": 2048,  # simulated
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Dispatch registry: tool_name (as used in policy.yaml / requests) -> callable
# ---------------------------------------------------------------------------
async def execute_tool(tool_name: str, parameters: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    if tool_name == "database_delete":
        return await DatabaseTool.delete(
            record_count=parameters.get("record_count"),
            table=parameters.get("table", "unspecified"),
            dry_run=dry_run,
        )
    if tool_name == "send_email":
        return await EmailTool.send(
            email_to=parameters.get("email_to"),
            subject=parameters.get("subject", "(no subject)"),
            body=parameters.get("body", ""),
            dry_run=dry_run,
        )
    if tool_name == "read_file":
        return await FileSystemTool.read(
            file_path=parameters.get("file_path"),
            dry_run=dry_run,
        )
    raise ToolExecutionError(f"No mock tool implementation registered for '{tool_name}'")
