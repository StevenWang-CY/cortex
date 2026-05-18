/**
 * Global setup for vitest specs.
 *
 * Each test file gets a fresh `chrome.*` fake and a fresh WebSocket
 * registry; we install them here before any background-script module
 * is imported. Tests can opt into deeper customisation by calling the
 * helpers directly.
 */

import { afterEach, beforeEach } from "vitest";
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
