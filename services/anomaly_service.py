from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from ..core.logging_center import audit_log
from ..repositories.diagnostics_repository import DiagnosticsRepository


class AnomalyService:
    """Business logic for anomaly tracking, pattern analysis and safe autofixes."""

    def __init__(self, repository: DiagnosticsRepository, logger: logging.Logger):
        self.repository = repository
        self.logger = logger
        self._fail_log_window_sec = 30.0
        self._fail_log_buckets: Dict[str, Dict[str, Any]] = {}

    async def record_api_request(
        self,
        *,
        endpoint: str,
        method: str,
        status_code: int,
        duration_ms: int,
        user_id: int = 0,
        error_type: str = "",
        payload: Optional[Dict[str, Any]] = None,
        note: str = "",
    ) -> None:
        await self.repository.record_event(
            event_kind="api_request",
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            duration_ms=duration_ms,
            user_id=user_id,
            error_type=error_type,
            payload=payload,
            note=note,
        )
        if status_code >= 400:
            key = f"{method}:{endpoint}:{status_code}:{error_type or '-'}"
            now = time.monotonic()
            bucket = self._fail_log_buckets.get(key)
            if not bucket:
                bucket = {
                    "window_start": now,
                    "count": 0,
                    "suppressed": 0,
                }
                self._fail_log_buckets[key] = bucket

            elapsed = now - float(bucket["window_start"])
            if elapsed >= self._fail_log_window_sec:
                suppressed = int(bucket["suppressed"])
                total = int(bucket["count"])
                if suppressed > 0:
                    audit_log(
                        self.logger,
                        "api.request.failed.summary",
                        endpoint=endpoint,
                        method=method,
                        status_code=status_code,
                        error_type=error_type,
                        total_failed=total,
                        suppressed=suppressed,
                        window_sec=int(self._fail_log_window_sec),
                    )
                bucket["window_start"] = now
                bucket["count"] = 0
                bucket["suppressed"] = 0

            bucket["count"] = int(bucket["count"]) + 1
            if int(bucket["count"]) == 1:
                audit_log(
                    self.logger,
                    "api.request.failed",
                    endpoint=endpoint,
                    method=method,
                    status_code=status_code,
                    user_id=user_id,
                    error_type=error_type,
                )
            else:
                bucket["suppressed"] = int(bucket["suppressed"]) + 1

    async def record_anomaly(
        self,
        *,
        endpoint: str,
        user_id: int,
        category: str,
        details: Optional[Dict[str, Any]] = None,
        note: str = "",
    ) -> None:
        await self.repository.record_event(
            event_kind="anomaly",
            endpoint=endpoint,
            method="SYSTEM",
            status_code=422,
            duration_ms=0,
            user_id=user_id,
            error_type=category,
            payload=details or {},
            note=note,
        )
        audit_log(
            self.logger,
            "api.anomaly",
            endpoint=endpoint,
            user_id=user_id,
            category=category,
            details=details or {},
            note=note,
        )

    async def build_report(
        self,
        *,
        user_id: int = 0,
        since_hours: int = 24,
        limit: int = 20,
    ) -> Dict[str, Any]:
        endpoint_failures = await self.repository.get_endpoint_failures(since_hours=since_hours, limit=limit)
        user_failures = await self.repository.get_user_failures(since_hours=since_hours, limit=limit)
        recent_errors = await self.repository.get_recent_errors(since_hours=since_hours, limit=limit * 2)
        event_count = await self.repository.get_event_count(since_hours=since_hours)

        owner_filter = user_id if user_id > 0 else None
        missing_archive = await self.repository.find_missing_media(
            "deleted_messages",
            owner_id=owner_filter,
            limit=limit * 4,
        )
        missing_thread = await self.repository.find_missing_media(
            "chat_thread_messages",
            owner_id=owner_filter,
            limit=limit * 4,
        )

        missing_total = len(missing_archive) + len(missing_thread)
        problematic_types = Counter()
        for item in missing_archive + missing_thread:
            ext = str(item.get("file_ext") or "").strip().lower()
            content_type = str(item.get("content_type") or "").strip().lower()
            key = ext or content_type or "unknown"
            problematic_types[key] += 1

        insights: List[str] = []
        if endpoint_failures:
            top_endpoint = endpoint_failures[0]
            if top_endpoint["fail_count"] >= 3:
                insights.append(
                    f"Endpoint {top_endpoint['endpoint']} is unstable: {top_endpoint['fail_count']} failed requests."
                )
        if user_failures:
            top_user = user_failures[0]
            if top_user["fail_count"] >= 3:
                insights.append(
                    f"User {top_user['user_id']} triggers frequent errors ({top_user['fail_count']} cases)."
                )
        if missing_total > 0:
            insights.append(f"Inconsistent media references detected: {missing_total} broken paths.")
        if problematic_types:
            bad_type, bad_count = problematic_types.most_common(1)[0]
            if bad_count > 1:
                insights.append(f"Most problematic media type: {bad_type} ({bad_count} missing files).")
        if not insights:
            insights.append("No recurring anomalies found in the selected interval.")

        return {
            "since_hours": int(since_hours),
            "tracked_events": event_count,
            "endpoint_failures": endpoint_failures,
            "user_failures": user_failures,
            "recent_errors": recent_errors,
            "missing_media": {
                "archive": missing_archive[:limit],
                "thread": missing_thread[:limit],
                "total": missing_total,
            },
            "problematic_types": [{"type": key, "count": count} for key, count in problematic_types.most_common(limit)],
            "insights": insights,
        }

    async def auto_fix_missing_media(
        self,
        *,
        user_id: int = 0,
        limit: int = 20,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        owner_filter = user_id if user_id > 0 else None
        candidates = (
            await self.repository.find_missing_media("deleted_messages", owner_id=owner_filter, limit=limit)
            + await self.repository.find_missing_media("chat_thread_messages", owner_id=owner_filter, limit=limit)
        )
        candidates = candidates[: max(1, int(limit))]

        fixed_items: List[Dict[str, Any]] = []
        for item in candidates:
            if dry_run:
                fixed_items.append(
                    {
                        "table_name": item["table_name"],
                        "item_id": item["item_id"],
                        "owner_id": item.get("owner_id"),
                        "before_value": item["media_path"],
                        "after_value": "",
                        "updated": False,
                    }
                )
                continue

            updated = await self.repository.clear_media_path(item["table_name"], item["item_id"])
            if not updated:
                continue

            fixed_items.append(updated)
            if updated.get("updated"):
                await self.repository.record_fix(
                    table_name=item["table_name"],
                    item_id=item["item_id"],
                    owner_id=item.get("owner_id"),
                    field_name="media_path",
                    before_value=str(updated.get("before_value") or ""),
                    after_value=str(updated.get("after_value") or ""),
                    reason="Missing media file path auto-fixed",
                )
                await self.repository.record_event(
                    event_kind="auto_fix",
                    endpoint="/ai/diagnostics/autofix",
                    method="SYSTEM",
                    status_code=200,
                    duration_ms=0,
                    user_id=int(item.get("owner_id") or 0),
                    error_type="missing_media_path",
                    payload={
                        "table_name": item["table_name"],
                        "item_id": item["item_id"],
                    },
                    note="media_path cleared due to missing file",
                )

        return {
            "dry_run": bool(dry_run),
            "checked": len(candidates),
            "fixed": sum(1 for item in fixed_items if item.get("updated")),
            "items": fixed_items,
        }

    async def auto_fix_single_missing_media(
        self,
        *,
        table_name: str,
        item_id: int,
        owner_id: int,
        media_path: str,
        reason: str,
    ) -> bool:
        updated = await self.repository.clear_media_path(table_name, item_id)
        if not updated or not updated.get("updated"):
            return False

        await self.repository.record_fix(
            table_name=table_name,
            item_id=item_id,
            owner_id=owner_id,
            field_name="media_path",
            before_value=str(media_path or ""),
            after_value="",
            reason=reason,
        )
        await self.repository.record_event(
            event_kind="auto_fix",
            endpoint="/ai/internal/media-consistency",
            method="SYSTEM",
            status_code=200,
            duration_ms=0,
            user_id=owner_id,
            error_type="missing_media_path",
            payload={"table_name": table_name, "item_id": item_id},
            note=reason,
        )
        audit_log(
            self.logger,
            "api.anomaly.autofix",
            table_name=table_name,
            item_id=item_id,
            owner_id=owner_id,
            reason=reason,
        )
        return True
