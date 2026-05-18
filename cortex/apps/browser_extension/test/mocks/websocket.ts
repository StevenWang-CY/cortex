/**
 * Controllable `WebSocket` mock for vitest tests.
 *
 * Tests instantiate `installFakeWebSocket()` once per case, capture
 * the latest socket via `getLatestSocket()`, then push canned inbound
 * frames with `socket.__deliver(...)` and inspect outbound sends via
 * `socket.sent`.
 */

import { vi } from "vitest";

export type FakeSocketState = "CONNECTING" | "OPEN" | "CLOSED";

export interface FakeWebSocket {
    url: string;
    readyState: number;
    onopen: ((ev: Event) => void) | null;
    onclose: ((ev: CloseEvent) => void) | null;
    onerror: ((ev: Event) => void) | null;
    onmessage: ((ev: MessageEvent) => void) | null;
    send: (data: string) => void;
    close: (code?: number, reason?: string) => void;
    sent: string[];
    closedCalls: Array<{ code?: number; reason?: string }>;
    __open: () => void;
    __deliver: (data: string | object) => void;
    __error: () => void;
    __remoteClose: (code?: number, reason?: string) => void;
}

interface FakeSocketRegistry {
    sockets: FakeWebSocket[];
    Ctor: typeof globalThis.WebSocket;
}

let registry: FakeSocketRegistry | null = null;

export function installFakeWebSocket(): FakeSocketRegistry {
    const sockets: FakeWebSocket[] = [];

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    function FakeCtor(this: any, url: string) {
        const sock: FakeWebSocket = {
            url,
            readyState: 0,
            onopen: null,
            onclose: null,
            onerror: null,
            onmessage: null,
            sent: [],
            closedCalls: [],
            send: vi.fn((data: string) => {
                if (sock.readyState !== 1) return;
                sock.sent.push(data);
            }) as unknown as (data: string) => void,
            close: vi.fn((code?: number, reason?: string) => {
                if (sock.readyState === 3) return;
                sock.readyState = 3;
                sock.closedCalls.push({ code, reason });
                queueMicrotask(() => {
                    if (sock.onclose) {
                        sock.onclose({
                            code: code ?? 1000,
                            reason: reason ?? "",
                            wasClean: true,
                        } as CloseEvent);
                    }
                });
            }) as unknown as (code?: number, reason?: string) => void,
            __open: () => {
                sock.readyState = 1;
                if (sock.onopen) sock.onopen(new Event("open"));
            },
            __deliver: (data: string | object) => {
                const str =
                    typeof data === "string" ? data : JSON.stringify(data);
                if (sock.onmessage) {
                    sock.onmessage({ data: str } as MessageEvent);
                }
            },
            __error: () => {
                if (sock.onerror) sock.onerror(new Event("error"));
            },
            __remoteClose: (code = 1006, reason = "") => {
                sock.readyState = 3;
                if (sock.onclose) {
                    sock.onclose({
                        code,
                        reason,
                        wasClean: code === 1000,
                    } as CloseEvent);
                }
            },
        };
        sockets.push(sock);
        // Auto-open on the next microtask so callers that synchronously
        // assign `.onopen` will see the event.
        queueMicrotask(() => {
            if (sock.readyState === 0) sock.__open();
        });
        return sock;
    }

    // Static fields expected on WebSocket.
    (FakeCtor as unknown as { CONNECTING: number }).CONNECTING = 0;
    (FakeCtor as unknown as { OPEN: number }).OPEN = 1;
    (FakeCtor as unknown as { CLOSING: number }).CLOSING = 2;
    (FakeCtor as unknown as { CLOSED: number }).CLOSED = 3;

    const prev = globalThis.WebSocket as typeof globalThis.WebSocket;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).WebSocket = FakeCtor as unknown as typeof globalThis.WebSocket;

    registry = { sockets, Ctor: prev };
    return { sockets, Ctor: prev };
}

export function getLatestSocket(): FakeWebSocket | null {
    if (!registry || registry.sockets.length === 0) return null;
    return registry.sockets[registry.sockets.length - 1];
}

export function getAllSockets(): FakeWebSocket[] {
    return registry?.sockets ?? [];
}

export function resetFakeWebSockets(): void {
    if (registry) registry.sockets.length = 0;
}

export function uninstallFakeWebSocket(): void {
    if (!registry) return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).WebSocket = registry.Ctor;
    registry = null;
}
