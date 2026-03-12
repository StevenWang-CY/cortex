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

import asyncio
import logging
import multiprocessing
import os
import signal
import sys
import time
from typing import Any

import uvicorn

from cortex.libs.config.settings import CortexConfig, get_config

logger = logging.getLogger(__name__)

# Services that run as async tasks within the main process
_ASYNC_SERVICES = [
    "capture_service",
    "physio_engine",
    "kinematics_engine",
    "telemetry_engine",
    "state_engine",
    "context_engine",
]


class ServiceProcess:
    """Wraps a service running in a subprocess."""

    def __init__(self, name: str, target: Any, args: tuple = ()) -> None:
        self.name = name
        self.target = target
        self.args = args
        self.process: multiprocessing.Process | None = None

    def start(self) -> None:
        self.process = multiprocessing.Process(
            target=self.target,
            args=self.args,
            name=f"cortex-{self.name}",
            daemon=True,
        )
        self.process.start()
        logger.info("Started %s (pid=%s)", self.name, self.process.pid)

    def stop(self, timeout: float = 5.0) -> None:
        if self.process is None or not self.process.is_alive():
            return
        self.process.terminate()
        self.process.join(timeout=timeout)
        if self.process.is_alive():
            logger.warning("Force killing %s", self.name)
            self.process.kill()
            self.process.join(timeout=2.0)
        logger.info("Stopped %s", self.name)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.is_alive()


def _run_api_server(config: CortexConfig) -> None:
    """Run the FastAPI/uvicorn API server in a subprocess."""
    from cortex.services.api_gateway.app import create_app

    app = create_app(config=config.api, cortex_config=config)
    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.port,
        log_level="info",
        access_log=False,
    )


def _run_ws_server(config: CortexConfig) -> None:
    """Run the WebSocket server in a subprocess."""
    from cortex.services.api_gateway.websocket_server import WebSocketServer

    server = WebSocketServer(
        host=config.api.host,
        port=config.api.ws_port,
    )

    async def _serve() -> None:
        await server.start()
        # Keep running until process is terminated
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()

    asyncio.run(_serve())


class DevServer:
    """
    Orchestrates all Cortex services for local development.

    - API Gateway runs as a subprocess (uvicorn)
    - WebSocket server runs as a subprocess
    - Other services run as async tasks in the main process
    """

    def __init__(self, config: CortexConfig | None = None) -> None:
        self.config = config or get_config()
        self._processes: list[ServiceProcess] = []
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

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

        # Start subprocess-based services
        api_proc = ServiceProcess(
            "api-gateway", _run_api_server, (self.config,)
        )
        ws_proc = ServiceProcess(
            "ws-server", _run_ws_server, (self.config,)
        )

        self._processes = [api_proc, ws_proc]
        for proc in self._processes:
            proc.start()

        # Give servers a moment to bind
        await asyncio.sleep(0.5)

        # Log service status
        logger.info(
            "Services started: api=%s ws=%s",
            api_proc.alive,
            ws_proc.alive,
        )
        logger.info(
            "Cortex dev server ready. Press Ctrl+C to stop."
        )

        # Wait for shutdown signal
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Stop all services gracefully."""
        logger.info("Shutting down Cortex services...")

        # Cancel async tasks
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Stop subprocess services
        for proc in reversed(self._processes):
            proc.stop()

        logger.info("All services stopped.")

    async def run(self) -> None:
        """Run the dev server until shutdown."""
        try:
            await self.start()
        finally:
            await self.stop()


def main() -> None:
    """Entry point for cortex-dev command."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = get_config()
    server = DevServer(config)

    print("=" * 60)
    print("  Cortex Development Server")
    print("=" * 60)
    print(f"  API:       http://{config.api.host}:{config.api.port}")
    print(f"  WebSocket: ws://{config.api.host}:{config.api.ws_port}")
    print(f"  LLM Mode:  {config.llm.mode}")
    print(f"  Capture:   device {config.capture.device_id} @ {config.capture.fps} FPS")
    print("=" * 60)

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
