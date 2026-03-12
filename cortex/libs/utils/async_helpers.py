"""
Cortex Async Utilities

Async queue wrappers, timeout helpers, and graceful shutdown utilities.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Generic, TypeVar

T = TypeVar("T")


class AsyncQueue(Generic[T]):
    """
    Thread-safe async queue wrapper with additional convenience methods.

    Useful for producer-consumer patterns in the sensing pipeline.
    """

    def __init__(self, maxsize: int = 0) -> None:
        """
        Initialize the async queue.

        Args:
            maxsize: Maximum queue size (0 = unlimited)
        """
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def put(self, item: T) -> None:
        """Put an item into the queue."""
        if self._closed:
            raise RuntimeError("Queue is closed")
        await self._queue.put(item)

    def put_nowait(self, item: T) -> None:
        """Put an item without waiting (may raise QueueFull)."""
        if self._closed:
            raise RuntimeError("Queue is closed")
        self._queue.put_nowait(item)

    async def get(self) -> T:
        """Get an item from the queue."""
        return await self._queue.get()

    def get_nowait(self) -> T:
        """Get an item without waiting (may raise QueueEmpty)."""
        return self._queue.get_nowait()

    async def get_timeout(self, timeout: float) -> T | None:
        """
        Get an item with timeout.

        Args:
            timeout: Timeout in seconds

        Returns:
            Item or None if timeout
        """
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def qsize(self) -> int:
        """Get current queue size."""
        return self._queue.qsize()

    def empty(self) -> bool:
        """Check if queue is empty."""
        return self._queue.empty()

    def full(self) -> bool:
        """Check if queue is full."""
        return self._queue.full()

    def close(self) -> None:
        """Mark queue as closed."""
        self._closed = True

    @property
    def is_closed(self) -> bool:
        """Check if queue is closed."""
        return self._closed

    async def drain(self) -> list[T]:
        """
        Drain all items from the queue.

        Returns:
            List of all items that were in the queue
        """
        items: list[T] = []
        while not self._queue.empty():
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def __aiter__(self) -> AsyncIterator[T]:
        """Iterate over queue items until closed."""
        while not self._closed:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                yield item
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break


async def with_timeout(
    coro: Any,
    timeout: float,
    default: T | None = None,
) -> T | None:
    """
    Execute a coroutine with timeout.

    Args:
        coro: Coroutine to execute
        timeout: Timeout in seconds
        default: Default value to return on timeout

    Returns:
        Coroutine result or default value
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return default


async def retry_async(
    func: Callable[[], Any],
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Any:
    """
    Retry an async function with exponential backoff.

    Args:
        func: Async function to retry
        max_retries: Maximum number of retries
        delay: Initial delay between retries
        backoff: Multiplier for delay after each retry
        exceptions: Exception types to catch and retry

    Returns:
        Function result

    Raises:
        Last exception if all retries fail
    """
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            if attempt < max_retries:
                await asyncio.sleep(delay * (backoff**attempt))

    if last_exception:
        raise last_exception


class GracefulShutdown:
    """
    Manager for graceful shutdown of async services.

    Usage:
        shutdown = GracefulShutdown()
        shutdown.register_handler(my_cleanup_func)

        async with shutdown:
            # Your service code here
            while not shutdown.should_exit:
                # Do work
                pass
    """

    def __init__(self) -> None:
        self._should_exit = False
        self._handlers: list[Callable[[], Any]] = []
        self._event = asyncio.Event()

    @property
    def should_exit(self) -> bool:
        """Check if shutdown has been requested."""
        return self._should_exit

    def request_exit(self) -> None:
        """Request graceful shutdown."""
        self._should_exit = True
        self._event.set()

    async def wait_for_exit(self) -> None:
        """Wait until shutdown is requested."""
        await self._event.wait()

    def register_handler(self, handler: Callable[[], Any]) -> None:
        """
        Register a cleanup handler to run on shutdown.

        Args:
            handler: Cleanup function (can be sync or async)
        """
        self._handlers.append(handler)

    async def _run_handlers(self) -> None:
        """Run all registered cleanup handlers."""
        for handler in reversed(self._handlers):
            try:
                result = handler()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # Log but don't fail shutdown

    def _signal_handler(self) -> None:
        """Handle shutdown signals."""
        self.request_exit()

    async def __aenter__(self) -> "GracefulShutdown":
        """Set up signal handlers."""
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._signal_handler)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Clean up on exit."""
        await self._run_handlers()


@asynccontextmanager
async def timeout_context(seconds: float) -> AsyncIterator[None]:
    """
    Context manager for timing out a block of code.

    Args:
        seconds: Timeout in seconds

    Raises:
        asyncio.TimeoutError if block takes too long
    """
    async with asyncio.timeout(seconds):
        yield


class RateLimiter:
    """
    Simple async rate limiter.

    Limits operations to a maximum rate per second.
    """

    def __init__(self, max_rate: float) -> None:
        """
        Initialize rate limiter.

        Args:
            max_rate: Maximum operations per second
        """
        self._interval = 1.0 / max_rate
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until rate limit allows next operation."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait_time = self._interval - (now - self._last_call)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self._last_call = asyncio.get_event_loop().time()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


class CircularBuffer(Generic[T]):
    """
    Fixed-size circular buffer for time-series data.

    Thread-safe for single producer, single consumer.
    """

    def __init__(self, maxlen: int) -> None:
        """
        Initialize circular buffer.

        Args:
            maxlen: Maximum number of items to store
        """
        self._maxlen = maxlen
        self._buffer: list[T] = []
        self._start = 0

    def append(self, item: T) -> None:
        """Add an item to the buffer."""
        if len(self._buffer) < self._maxlen:
            self._buffer.append(item)
        else:
            self._buffer[self._start] = item
            self._start = (self._start + 1) % self._maxlen

    def __len__(self) -> int:
        return len(self._buffer)

    def __getitem__(self, index: int) -> T:
        if index < 0 or index >= len(self._buffer):
            raise IndexError("Index out of range")
        actual_index = (self._start + index) % len(self._buffer)
        return self._buffer[actual_index]

    def to_list(self) -> list[T]:
        """Get buffer contents as a list (oldest first)."""
        if len(self._buffer) < self._maxlen:
            return list(self._buffer)
        return self._buffer[self._start:] + self._buffer[: self._start]

    def clear(self) -> None:
        """Clear the buffer."""
        self._buffer = []
        self._start = 0

    @property
    def is_full(self) -> bool:
        """Check if buffer is full."""
        return len(self._buffer) >= self._maxlen
