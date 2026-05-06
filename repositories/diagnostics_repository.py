from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

ALLOWED_MEDIA_TABLES = {"deleted_messages", "chat_thread_messages"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiagnosticsRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_schema(self) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_diagnostics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    method TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    error_type TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS data_fix_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    owner_id INTEGER,
                    field_name TEXT NOT NULL,
                    before_value TEXT,
                    after_value TEXT,
                    reason TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_diag_events_created ON api_diagnostics_events(created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_diag_events_endpoint ON api_diagnostics_events(endpoint, status_code)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_diag_events_user ON api_diagnostics_events(user_id, status_code)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_data_fix_created ON data_fix_log(created_at)"
            )
            await conn.commit()

    async def record_event(
        self,
        *,
        event_kind: str,
        endpoint: str,
        method: str,
        status_code: int,
        duration_ms: int,
        user_id: int = 0,
        error_type: str = "",
        payload: Optional[Dict[str, Any]] = None,
        note: str = "",
    ) -> None:
        payload_json = "{}"
        if payload:
            payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO api_diagnostics_events (
                    created_at, event_kind, endpoint, method, status_code, duration_ms,
                    user_id, error_type, payload_json, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    str(event_kind or "unknown"),
                    str(endpoint or ""),
                    str(method or "POST"),
                    int(status_code or 0),
                    int(duration_ms or 0),
                    int(user_id or 0),
                    str(error_type or ""),
                    payload_json,
                    str(note or ""),
                ),
            )
            await conn.commit()

    async def record_fix(
        self,
        *,
        table_name: str,
        item_id: int,
        owner_id: Optional[int],
        field_name: str,
        before_value: Optional[str],
        after_value: Optional[str],
        reason: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO data_fix_log (
                    created_at, table_name, item_id, owner_id, field_name,
                    before_value, after_value, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    str(table_name),
                    int(item_id),
                    int(owner_id) if owner_id is not None else None,
                    str(field_name),
                    before_value,
                    after_value,
                    str(reason),
                ),
            )
            await conn.commit()

    async def get_endpoint_failures(self, *, since_hours: int = 24, limit: int = 20) -> List[Dict[str, Any]]:
        since_ts = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(since_hours)))).isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT endpoint, COUNT(*) AS fail_count,
                       SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS server_error_count
                FROM api_diagnostics_events
                WHERE created_at >= ?
                  AND status_code >= 400
                GROUP BY endpoint
                ORDER BY fail_count DESC
                LIMIT ?
                """,
                (since_ts, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "endpoint": str(row["endpoint"] or ""),
                "fail_count": int(row["fail_count"] or 0),
                "server_error_count": int(row["server_error_count"] or 0),
            }
            for row in rows
        ]

    async def get_user_failures(self, *, since_hours: int = 24, limit: int = 20) -> List[Dict[str, Any]]:
        since_ts = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(since_hours)))).isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT user_id, COUNT(*) AS fail_count
                FROM api_diagnostics_events
                WHERE created_at >= ?
                  AND user_id > 0
                  AND status_code >= 400
                GROUP BY user_id
                ORDER BY fail_count DESC
                LIMIT ?
                """,
                (since_ts, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [{"user_id": int(row["user_id"]), "fail_count": int(row["fail_count"] or 0)} for row in rows]

    async def get_recent_errors(self, *, since_hours: int = 24, limit: int = 40) -> List[Dict[str, Any]]:
        since_ts = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(since_hours)))).isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT created_at, endpoint, method, status_code, user_id, error_type, note
                FROM api_diagnostics_events
                WHERE created_at >= ?
                  AND status_code >= 400
                ORDER BY id DESC
                LIMIT ?
                """,
                (since_ts, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "created_at": str(row["created_at"] or ""),
                "endpoint": str(row["endpoint"] or ""),
                "method": str(row["method"] or ""),
                "status_code": int(row["status_code"] or 0),
                "user_id": int(row["user_id"] or 0),
                "error_type": str(row["error_type"] or ""),
                "note": str(row["note"] or ""),
            }
            for row in rows
        ]

    async def get_event_count(self, *, since_hours: int = 24) -> int:
        since_ts = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(since_hours)))).isoformat()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM api_diagnostics_events WHERE created_at >= ?",
                (since_ts,),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def find_missing_media(
        self,
        table_name: str,
        *,
        owner_id: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if table_name not in ALLOWED_MEDIA_TABLES:
            raise ValueError(f"Unsupported table {table_name!r}")

        where = "TRIM(COALESCE(media_path, '')) <> ''"
        params: List[Any] = []
        if owner_id is not None:
            where += " AND owner_id = ?"
            params.append(int(owner_id))
        params.append(int(limit))

        query = (
            f"SELECT id, owner_id, COALESCE(content_type, '') AS content_type, COALESCE(media_path, '') AS media_path "
            f"FROM {table_name} WHERE {where} ORDER BY id DESC LIMIT ?"
        )

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(query, tuple(params)) as cur:
                rows = await cur.fetchall()

        missing: List[Dict[str, Any]] = []
        for row in rows:
            media_path = str(row["media_path"] or "").strip()
            if not media_path:
                continue
            if os.path.exists(media_path):
                continue
            missing.append(
                {
                    "table_name": table_name,
                    "item_id": int(row["id"]),
                    "owner_id": int(row["owner_id"]) if row["owner_id"] is not None else None,
                    "content_type": str(row["content_type"] or ""),
                    "media_path": media_path,
                    "file_ext": os.path.splitext(media_path)[1].lower(),
                }
            )
        return missing

    async def clear_media_path(self, table_name: str, item_id: int) -> Optional[Dict[str, Any]]:
        if table_name not in ALLOWED_MEDIA_TABLES:
            raise ValueError(f"Unsupported table {table_name!r}")

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                f"SELECT id, owner_id, COALESCE(media_path, '') AS media_path FROM {table_name} WHERE id = ? LIMIT 1",
                (int(item_id),),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None

            before_value = str(row["media_path"] or "")
            if not before_value:
                return {
                    "table_name": table_name,
                    "item_id": int(row["id"]),
                    "owner_id": int(row["owner_id"]) if row["owner_id"] is not None else None,
                    "before_value": before_value,
                    "after_value": "",
                    "updated": False,
                }

            await conn.execute(f"UPDATE {table_name} SET media_path = '' WHERE id = ?", (int(item_id),))
            await conn.commit()
            return {
                "table_name": table_name,
                "item_id": int(row["id"]),
                "owner_id": int(row["owner_id"]) if row["owner_id"] is not None else None,
                "before_value": before_value,
                "after_value": "",
                "updated": True,
            }
