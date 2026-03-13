"""
Context Engine — Browser Adapter

Communicates with the Chrome extension via WebSocket to gather
BrowserContext (active tab, all tabs, content excerpt, tab classification).

When the extension is unavailable, falls back gracefully with None.

Tab type classification uses URL-based heuristics:
- stackoverflow: stackoverflow.com, stackexchange.com
- documentation: docs.*, MDN, readthedocs, framework docs
- search: google/bing/duckduckgo search results
- code_host: github, gitlab, bitbucket
- social: twitter, reddit, youtube, etc.
- other: everything else
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from cortex.libs.schemas.context import BrowserContext, TabInfo
from cortex.services.context_engine.app_classifier import classify_tab_type

logger = logging.getLogger(__name__)


class BrowserAdapter:
    """
    Adapter for gathering Chrome browser context.

    Connects to the Chrome extension via WebSocket and requests
    current tab state and active-tab content. Falls back to None
    when the extension is unavailable.

    Usage:
        adapter = BrowserAdapter(ws_send_fn=send, ws_receive_fn=receive)
        ctx = await adapter.get_context(timeout=2.0)
    """

    def __init__(
        self,
        ws_send_fn: Any | None = None,
        ws_receive_fn: Any | None = None,
        request_context_fn: Any | None = None,
    ) -> None:
        self._ws_send = ws_send_fn
        self._ws_receive = ws_receive_fn
        self._request_context = request_context_fn
        self._available = False
        self._last_context: BrowserContext | None = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_context(self) -> BrowserContext | None:
        return self._last_context

    async def get_context(self, timeout: float = 2.0) -> BrowserContext | None:
        """
        Request browser context from Chrome extension.

        Args:
            timeout: Maximum seconds to wait for response.

        Returns:
            BrowserContext if extension responds, None otherwise.
        """
        if self._request_context is not None:
            try:
                payload = await asyncio.wait_for(self._request_context("chrome"), timeout=timeout)
                if not isinstance(payload, dict):
                    self._available = False
                    return None
                browser_payload = payload.get("browser_context", payload)
                ctx = self._parse_browser_context(
                    browser_payload if isinstance(browser_payload, dict) else {}
                )
                self._available = bool(ctx.active_tab_title or ctx.active_tab_url or ctx.all_tabs)
                self._last_context = ctx if self._available else None
                return self._last_context
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                logger.debug(f"Browser adapter unavailable: {e}")
                self._available = False
                return None

        if self._ws_send is None or self._ws_receive is None:
            self._available = False
            return None

        try:
            request = json.dumps({
                "type": "CONTEXT_REQUEST",
                "payload": {"commands": [
                    "cortex.getActiveTabs",
                    "cortex.getActiveTabContent",
                ]},
            })

            await asyncio.wait_for(self._ws_send(request), timeout=timeout)
            raw = await asyncio.wait_for(self._ws_receive(), timeout=timeout)
            data = json.loads(raw)

            if data.get("type") != "CONTEXT_RESPONSE":
                self._available = False
                return None

            ctx = self._parse_browser_context(data.get("payload", {}))
            self._available = True
            self._last_context = ctx
            return ctx

        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            logger.debug(f"Browser adapter unavailable: {e}")
            self._available = False
            return None
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Invalid browser context response: {e}")
            self._available = False
            return None

    def update_from_payload(self, payload: dict) -> BrowserContext | None:
        """
        Update context directly from a payload dict.

        Args:
            payload: Browser context payload dict.

        Returns:
            Parsed BrowserContext, or None on parse error.
        """
        try:
            ctx = self._parse_browser_context(payload)
            self._available = True
            self._last_context = ctx
            return ctx
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Failed to parse browser context: {e}")
            return None

    @staticmethod
    def _parse_browser_context(payload: dict) -> BrowserContext:
        """Parse a BrowserContext from a payload dict."""
        tabs_raw = payload.get("all_tabs", [])
        tabs: list[TabInfo] = []
        type_counts: dict[str, int] = {}

        for t in tabs_raw:
            url = t.get("url", "")
            tab_type = t.get("tab_type") or classify_tab_type(url)
            is_active = t.get("is_active", False)

            tabs.append(TabInfo(
                title=t.get("title", ""),
                url=url,
                tab_type=tab_type,
                is_active=is_active,
            ))

            type_counts[tab_type] = type_counts.get(tab_type, 0) + 1

        # Truncate active tab content to 2000 tokens (~8000 chars)
        content = payload.get("active_tab_content_excerpt", "")
        if len(content) > 8000:
            content = content[:8000]

        return BrowserContext(
            active_tab_title=payload.get("active_tab_title", ""),
            active_tab_url=payload.get("active_tab_url", ""),
            active_tab_content_excerpt=content,
            all_tabs=tabs,
            tab_type_classification=type_counts,
        )

    def reset(self) -> None:
        """Reset adapter state."""
        self._available = False
        self._last_context = None
