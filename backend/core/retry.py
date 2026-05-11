"""Retry helpers with exponential backoff for async operations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Tuple, Type


@dataclass(frozen=True)
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    base_delay: float = 2.0
    max_delay: float = 30.0
    exponential_base: float = 2.0


ANTHROPIC_RETRY = RetryConfig(max_attempts=3, base_delay=5.0)
REDIS_RETRY = RetryConfig(max_attempts=5, base_delay=1.0)
SEARCH_RETRY = RetryConfig(max_attempts=2, base_delay=2.0)


async def retry_with_backoff(
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    config: RetryConfig,
    retryable_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    **kwargs: Any,
) -> Any:
    """Retry an async function with exponential backoff."""

    logger = logging.getLogger("researchswarm.retry")
    attempt = 0
    last_error: BaseException | None = None

    while attempt < config.max_attempts:
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as exc:  # pragma: no cover - error path
            attempt += 1
            last_error = exc
            if attempt >= config.max_attempts:
                break
            delay = min(
                config.base_delay * (config.exponential_base ** (attempt - 1)),
                config.max_delay,
            )
            logger.warning(
                "Retrying %s after error: %s (attempt %s/%s, delay %.2fs)",
                getattr(func, "__name__", "operation"),
                exc,
                attempt,
                config.max_attempts,
                delay,
            )
            await asyncio.sleep(delay)

    if last_error is None:
        raise RuntimeError("Retry attempts exhausted without error details")
    raise last_error
