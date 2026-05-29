"""
Cortex Development Server — Start All Services

Launches all Cortex services (capture, physio, kinematics, telemetry,
state, context, API gateway) with multiprocessing + asyncio.
Handles graceful shutdown via SIGINT/SIGTERM.

Usage:
    python -m cortex.scripts.run_dev
    cortex-dev  # if installed via pip
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time

from cortex.libs.config.settings import CortexConfig, get_config
from cortex.libs.logging import configure_logging
from cortex.services.capture_service.webcam import describe_requested_camera
from cortex.services.runtime_daemon import CortexDaemon

logger = logging.getLogger(__name__)


# audit Phase-I: startup-profile milestones. Each call to
# :func:`record_milestone` appends ``(label, monotonic_time_s)`` to this
# list. ``--profile-startup`` prints the table on exit. The module-level
# list is OK: ``run_dev`` runs as a single process and the daemon's
# milestones happen on the asyncio loop, so there is no concurrency.
_STARTUP_MILESTONES: list[tuple[str, float]] = []
_PROFILE_STARTUP_ENABLED = False


def record_milestone(label: str) -> None:
    """Record a startup-latency milestone (no-op unless --profile-startup).

    Called from key points in the daemon boot sequence
    (config-loaded, registry-built, capture-started, ws-listening,
    first-broadcast) so the table printed at exit covers the path
    from ``python -m cortex.scripts.run_dev`` to first broadcast.
    """
    if _PROFILE_STARTUP_ENABLED:
        _STARTUP_MILESTONES.append((label, time.monotonic()))


def _print_startup_profile() -> None:
    """Print the recorded startup milestones as a table."""
    if not _STARTUP_MILESTONES:
        return
    t0 = _STARTUP_MILESTONES[0][1]
    print()
    print("=" * 60)
    print("  Cortex startup profile (audit Phase-I)")
    print("=" * 60)
    print(f"  {'milestone':<28} {'elapsed':>10} {'delta':>10}")
    print(f"  {'-' * 28} {'-' * 10} {'-' * 10}")
    prev = t0
    for label, t in _STARTUP_MILESTONES:
        elapsed = t - t0
        delta = t - prev
        print(f"  {label:<28} {elapsed * 1000:>8.0f}ms {delta * 1000:>8.0f}ms")
        prev = t
    print("=" * 60)

class DevServer:
    """
    Orchestrates all Cortex services for local development.

    - API Gateway runs as a subprocess (uvicorn)
    - WebSocket server runs as a subprocess
    - Other services run as async tasks in the main process
    """

    def __init__(self, config: CortexConfig | None = None) -> None:
        self.config = config or get_config()
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._daemon = CortexDaemon(self.config)

    def _setup_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal)

    def _handle_signal(self) -> None:
        logger.info("Shutdown signal received")
        self._shutdown_event.set()

    async def start(self) -> None:
        """Start all services."""
        self._setup_signal_handlers()

        logger.info("Starting Cortex development server...")
        logger.info(
            "API: http://%s:%d  WS: ws://%s:%d",
            self.config.api.host,
            self.config.api.port,
            self.config.api.host,
            self.config.api.ws_port,
        )

        record_milestone("daemon-task-spawned")
        self._tasks = [
            asyncio.create_task(self._daemon.start(), name="cortex-daemon"),
        ]
        await asyncio.sleep(0.5)
        record_milestone("daemon-warmup-elapsed")
        logger.info("Services started: daemon=%s", True)
        logger.info(
            "Cortex dev server ready. Press Ctrl+C to stop."
        )

        # Wait for shutdown signal
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Stop all services gracefully."""
        logger.info("Shutting down Cortex services...")

        await self._daemon.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("All services stopped.")

    async def run(self) -> None:
        """Run the dev server until shutdown."""
        try:
            await self.start()
        finally:
            await self.stop()


def main() -> None:
    """Entry point for cortex-dev command."""
    global _PROFILE_STARTUP_ENABLED

    # C6 (audit): install the structlog processor chain BEFORE any logger
    # is used so dev-server log records flow through the same pipeline as
    # the daemon (correlation ids, service context, level routing). This
    # replaces the bare ``logging.basicConfig`` call that previously left
    # structlog unconfigured. Console renderer (json_format=False) keeps
    # the terminal output human-readable for local development.
    configure_logging(level="INFO", json_format=False)

    parser = argparse.ArgumentParser(
        prog="cortex-dev",
        description="Cortex development server",
    )
    parser.add_argument(
        "--profile-startup",
        action="store_true",
        help=(
            "Record startup-latency milestones (config-loaded, "
            "registry-built, capture-started, ws-listening, "
            "first-broadcast) and print the table on exit. audit Phase-I."
        ),
    )
    args, _unknown = parser.parse_known_args()
    _PROFILE_STARTUP_ENABLED = args.profile_startup
    record_milestone("entrypoint")

    config = get_config()
    record_milestone("config-loaded")
    server = DevServer(config)
    record_milestone("server-built")

    print("=" * 60)
    print("  Cortex Development Server")
    print("=" * 60)
    print(f"  API:       http://{config.api.host}:{config.api.port}")
    print(f"  WebSocket: ws://{config.api.host}:{config.api.ws_port}")
    print(
        f"  LLM:       {config.llm.provider} "
        f"(default model: {config.llm.model_default})"
    )
    print(
        f"  Capture:   device {describe_requested_camera(config.capture)} @ "
        f"{config.capture.fps} FPS"
    )
    print("=" * 60)

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
    finally:
        if _PROFILE_STARTUP_ENABLED:
            _print_startup_profile()


if __name__ == "__main__":
    main()
