"""
LLM Engine — Remote Qwen-3-8B Client

Connects to a vLLM / SGLang server via an SSH tunnel,
using the OpenAI-compatible chat/completions API over httpx.

Features:
- SSH tunnel lifecycle management (subprocess)
- OpenAI-compatible /v1/chat/completions via httpx
- Configurable timeout (default 10s) and retry (2x)
- Falls back to rule-based plan on failure
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any

import httpx

from cortex.libs.config.settings import LLMConfig
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, SimplificationConstraints
from cortex.libs.schemas.state import StateEstimate
from cortex.services.llm_engine.cache import LLMCache
from cortex.services.llm_engine.client import LLMError, build_fallback_plan
from cortex.services.llm_engine.parser import parse_and_validate
from cortex.services.llm_engine.prompts import build_messages

logger = logging.getLogger(__name__)


class RemoteQwenClient:
    """
    LLM client that talks to a remote Qwen-3-8B via OpenAI-compatible API.

    Implements the LLMClient protocol.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        if config is None:
            config = LLMConfig()
        self._config = config
        self._cache = cache or LLMCache()
        self._tunnel_process: subprocess.Popen[bytes] | None = None
        self._max_retries = 2

    # ------------------------------------------------------------------
    # SSH Tunnel Management
    # ------------------------------------------------------------------

    async def open_tunnel(self) -> bool:
        """
        Open an SSH tunnel to the remote LLM server.

        Returns True if tunnel was started (or already running).
        """
        if not self._config.remote.ssh_tunnel:
            return True  # No tunnel needed

        if self._tunnel_process is not None and self._tunnel_process.poll() is None:
            return True  # Already running

        cmd = [
            "ssh",
            "-N",  # No remote command
            "-L", f"{self._config.remote.port}:localhost:{self._config.remote.port}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            f"{self._config.remote.ssh_user}@{self._config.remote.host}",
        ]

        try:
            self._tunnel_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            # Give the tunnel a moment to establish
            await asyncio.sleep(1.0)
            if self._tunnel_process.poll() is not None:
                stderr = ""
                if self._tunnel_process.stderr is not None:
                    stderr = self._tunnel_process.stderr.read().decode(
                        "utf-8", errors="replace"
                    ).strip()
                logger.error(
                    "SSH tunnel exited immediately%s",
                    f": {stderr}" if stderr else "",
                )
                self._tunnel_process = None
                return False
            logger.info("SSH tunnel established to %s", self._config.remote.host)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Failed to open SSH tunnel: %s", exc)
            return False

    async def close_tunnel(self) -> None:
        """Close the SSH tunnel if it's running."""
        if self._tunnel_process is not None:
            self._tunnel_process.terminate()
            try:
                self._tunnel_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tunnel_process.kill()
            self._tunnel_process = None
            logger.info("SSH tunnel closed")

    @property
    def tunnel_active(self) -> bool:
        """Check if the SSH tunnel is currently running."""
        return (
            self._tunnel_process is not None
            and self._tunnel_process.poll() is None
        )

    # ------------------------------------------------------------------
    # LLMClient Protocol
    # ------------------------------------------------------------------

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
    ) -> InterventionPlan:
        """Generate an intervention plan via the remote Qwen model."""
        # Check cache first
        cached = self._cache.get(context, state, constraints)
        if cached is not None:
            return cached

        messages = build_messages(
            context,
            state,
            constraints,
            template_name=template_name,
            extra_context=extra_context,
        )

        # Retry loop
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                if self._config.remote.ssh_tunnel and not self.tunnel_active:
                    if not await self.open_tunnel():
                        raise OSError("failed to establish SSH tunnel")
                raw_response = await self._call_api(messages)
                plan = parse_and_validate(raw_response)
                if plan is not None:
                    self._cache.put(context, plan, state, constraints)
                    return plan
                logger.warning(
                    "Parse/validate failed on attempt %d: %s",
                    attempt,
                    raw_response[:200] if raw_response else "<empty>",
                )
            except (httpx.HTTPError, TimeoutError, OSError) as exc:
                last_error = exc
                logger.warning(
                    "API call failed on attempt %d: %s", attempt, exc
                )

        # All retries exhausted — fallback
        logger.error(
            "LLM call failed after %d retries, using fallback plan", self._max_retries
        )
        if last_error is not None:
            logger.error("Last error: %s", last_error)

        fallback = build_fallback_plan(context)
        return fallback

    async def health_check(self) -> bool:
        """Check if the remote LLM server is reachable."""
        try:
            if self._config.remote.ssh_tunnel and not self.tunnel_active:
                if not await self.open_tunnel():
                    return False
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._api_base_url}/v1/models")
                return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    # ------------------------------------------------------------------
    # Internal API call
    # ------------------------------------------------------------------

    async def _call_api(self, messages: list[dict[str, str]]) -> str:
        """
        Make a single call to the OpenAI-compatible chat API.

        Returns the raw content string from the first choice.
        """
        payload: dict[str, Any] = {
            "model": self._config.model_name,
            "messages": messages,
            "max_tokens": self._config.max_tokens,
            "temperature": self._config.temperature,
            "stream": False,
            "response_format": {"type": "json_object"},
        }

        timeout = httpx.Timeout(
            self._config.timeout_seconds,
            connect=5.0,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._api_base_url}/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise LLMError("No choices in API response")

        message = choices[0].get("message", {})
        content = self._extract_content(message.get("content"))
        if not content:
            content = self._extract_content(choices[0].get("text"))
        if not content:
            raise LLMError("Empty content in API response")

        return content

    @property
    def _api_base_url(self) -> str:
        host = "localhost" if self._config.remote.ssh_tunnel else self._config.remote.host
        return f"http://{host}:{self._config.remote.port}"

    @staticmethod
    def _extract_content(content: Any) -> str:
        """Normalize OpenAI-compatible content payloads into plain text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                else:
                    parts.append(str(item))
            return "".join(parts)
        if content is None:
            return ""
        if isinstance(content, (dict, tuple)):
            return json.dumps(content)
        return str(content)
