/**
 * Global setup for vitest specs.
 *
 * Each test file gets a fresh `chrome.*` fake and a fresh WebSocket
 * registry; we install them here before any background-script module
 * is imported. Tests can opt into deeper customisation by calling the
 * helpers directly.
 */

import { afterEach, beforeEach, vi } from "vitest";

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

// jsdom deliberately leaves CanvasRenderingContext2D unimplemented and logs
// an error before returning null. The new-tab animation already treats a null
// context as a graceful no-animation fallback; tests use this deterministic
// surface so mounting the component remains quiet and can exercise cleanup.
if (typeof HTMLCanvasElement !== "undefined") {
    const canvasContext = {
        arc: vi.fn(),
        beginPath: vi.fn(),
        clearRect: vi.fn(),
        fill: vi.fn(),
        fillRect: vi.fn(),
        scale: vi.fn(),
        stroke: vi.fn(),
        fillStyle: "",
        globalAlpha: 1,
        lineWidth: 1,
        strokeStyle: "",
    } as unknown as CanvasRenderingContext2D;
    Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
        configurable: true,
        value: vi.fn(() => canvasContext),
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
