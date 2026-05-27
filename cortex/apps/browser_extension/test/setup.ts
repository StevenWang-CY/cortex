/**
 * Global setup for vitest specs.
 *
 * Each test file gets a fresh `chrome.*` fake and a fresh WebSocket
 * registry; we install them here before any background-script module
 * is imported. Tests can opt into deeper customisation by calling the
 * helpers directly.
 */

import { afterEach, beforeEach } from "vitest";

// Suppress React 18 act() warning during component-mount tests; vitest
// runs them synchronously and we wrap explicit state changes already.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

// P2-7: jsdom does not implement window.matchMedia. Install a minimal
// stub so newtab.tsx's ``prefers-reduced-motion`` query does not throw.
if (typeof window !== "undefined" && !window.matchMedia) {
    Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: (query: string) => ({
            matches: false,
            media: query,
            onchange: null,
            addListener: () => {},
            removeListener: () => {},
            addEventListener: () => {},
            removeEventListener: () => {},
            dispatchEvent: () => false,
        }),
    });
}
import {
    installChromeFake,
    resetChromeFake,
    type ChromeFake,
} from "./mocks/chrome";
import {
    installFakeWebSocket,
    resetFakeWebSockets,
    uninstallFakeWebSocket,
} from "./mocks/websocket";

declare global {
    // eslint-disable-next-line no-var
    var __cortexChrome: ChromeFake;
}

beforeEach(() => {
    globalThis.__cortexChrome = installChromeFake();
    installFakeWebSocket();
});

afterEach(() => {
    resetChromeFake(globalThis.__cortexChrome);
    resetFakeWebSockets();
    uninstallFakeWebSocket();
});
