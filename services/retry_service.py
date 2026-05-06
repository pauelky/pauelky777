from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, Tuple, Type


class RetryService:
    """Generic async retry helper with exponential backoff."""

    def __init__(self, logger: logging.Logger, *, max_delay: float = 8.0):
        self.logger = logger
        self.max_delay = max(0.1, float(max_delay))

    async def execute(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        operation_name: str,
        attempts: int = 3,
        base_delay: float = 0.4,
        retry_for: Tuple[Type[BaseException], ...] = (Exception,),
        should_retry: Optional[Callable[[BaseException], bool]] = None,
    ) -> Any:
        max_attempts = max(1, int(attempts))
        delay = max(0.05, float(base_delay))

        for attempt in range(1, max_attempts + 1):
            try:
                return await operation()
            except BaseException as exc:  # noqa: BLE001
                is_retryable_type = isinstance(exc, retry_for)
                passes_custom_filter = bool(should_retry(exc)) if should_retry else True
                can_retry = attempt < max_attempts and is_retryable_type and passes_custom_filter
                if not can_retry:
                    raise

                sleep_for = min(delay * (2 ** (attempt - 1)), self.max_delay)
                self.logger.warning(
                    "RetryService: %s failed (%s), attempt %s/%s, retry in %.2fs",
                    operation_name,
                    type(exc).__name__,
                    attempt,
                    max_attempts,
                    sleep_for,
                )
                await asyncio.sleep(sleep_for)
