/**
 * A timed quiet mode ends at its ``ends_at`` even when no "off" frame
 * arrives.
 *
 * The daemon broadcasts the lapse of a snooze only to clients connected
 * at that moment, and a restarted daemon lets a timed window lapse
 * without telling anyone (it pushes only the latest STATE_UPDATE on
 * connect). A snooze that ended while the extension was disconnected —
 * Cortex stopped mid-snooze, a Mac asleep, a worker evicted — therefore
 * stayed on: the popup kept "Snooze 15m" selected and read "Snoozed"
 * indefinitely, the badge and OS notification stayed suppressed, and the
 * pause shortcut sent "off" (a no-op) instead of pausing. The daemon's
 * own ``get_quiet_mode_state`` treats a passed ``ends_at`` as "off"; the
 * clients now do the same. An indefinite pause has no ``ends_at`` and is
 * unaffected.
 */

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { within } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

let cleanup: (() => Promise<void>) | null = null;

afterEach(async () => {
    if (cleanup) await cleanup();
    cleanup = null;
    document.body.innerHTML = "";
});

type BgListener = (msg: Record<string, unknown>) => void;

async function mountPopup(): Promise<{ container: HTMLElement; listener: BgListener }> {
    const fake = globalThis.__cortexChrome;
    fake.runtime.sendMessage.mockImplementation(
        (message: Record<string, unknown>, callback?: (value: unknown) => void) => {
            if (message.type === "GET_STATE") {
                callback?.({ connected: true, state: null, intervention: null, focusSession: null });
            } else if (message.type === "GET_CACHED_RECAP") {
                callback?.({ recap: null, timestamp: null });
            } else {
                callback?.(undefined);
            }
            return Promise.resolve(undefined);
        },
    );
    const { default: CortexPopup } = await import("../popup");
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => root.render(<CortexPopup />));
    cleanup = async () => {
        await act(async () => root.unmount());
        container.remove();
    };
    const calls = fake.runtime.onMessage.addListener.mock.calls;
    return { container, listener: calls[calls.length - 1][0] as BgListener };
}

function quietView(container: HTMLElement) {
    const group = within(container).getByTestId("quiet-mode-control");
    return {
        status: within(container).getByTestId("quiet-mode-status").textContent,
        off: within(group).getByTestId("quiet-mode-off").getAttribute("aria-checked"),
        snooze: within(group).getByTestId("quiet-mode-snooze_15").getAttribute("aria-checked"),
    };
}

describe("popup: a lapsed snooze reads as off", () => {
    it("a cached snooze whose end has passed is not shown as active", async () => {
        globalThis.__cortexChrome.storage.session.__reset({
            quietMode: true,
            cortex_quiet_state: {
                kind: "snooze_15",
                ends_at_unix_ms: Date.now() - 60_000,
                duration_minutes: 15,
            },
        });
        const { container } = await mountPopup();
        expect(quietView(container)).toEqual({ status: "", off: "true", snooze: "false" });
    });

    it("a live snooze clears at its end without an off frame", async () => {
        const { container, listener } = await mountPopup();
        await act(async () => listener({
            type: "QUIET_MODE_STATE",
            payload: { kind: "snooze_15", ends_at_unix_ms: Date.now() + 150, duration_minutes: 1 },
        }));
        expect(quietView(container).snooze).toBe("true");

        await act(async () => { await new Promise((r) => setTimeout(r, 400)); });
        expect(quietView(container)).toEqual({ status: "", off: "true", snooze: "false" });
    });

    it("an indefinite pause stays on", async () => {
        const { container, listener } = await mountPopup();
        await act(async () => listener({
            type: "QUIET_MODE_STATE",
            payload: { kind: "pause", ends_at_unix_ms: null, ends_at: null, duration_minutes: null },
        }));
        await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
        expect(quietView(container).status).toBe("Paused");
    });
});

describe("background: the pause shortcut after a lapsed snooze", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    async function boot() {
        vi.resetModules();
        await import("../background");
        await new Promise((r) => setTimeout(r, 0));
        const socket = getLatestSocket()!;
        socket.__open();
        socket.__deliver({
            type: "AUTH_OK",
            payload: {},
            sequence: 1,
            timestamp: Date.now() / 1000,
            correlation_id: null,
            target_client_types: null,
            source_client_type: "daemon",
        });
        return socket;
    }

    function toggles(sent: string[]): Array<Record<string, unknown>> {
        return sent
            .map((raw) => JSON.parse(raw) as { type: string; payload: Record<string, unknown> })
            .filter((frame) => frame.type === "QUIET_MODE_TOGGLE")
            .map((frame) => frame.payload);
    }

    it("pauses instead of sending a no-op off", async () => {
        const socket = await boot();
        socket.__deliver({
            type: "QUIET_MODE_STATE",
            payload: { kind: "snooze_15", ends_at_unix_ms: Date.now() + 100, duration_minutes: 1 },
            sequence: 2,
            timestamp: Date.now() / 1000,
        });
        await new Promise((r) => setTimeout(r, 200));
        socket.sent.length = 0;

        globalThis.__cortexChrome.commands.onCommand.__dispatch("pause-cortex");
        await new Promise((r) => setTimeout(r, 20));

        expect(toggles(socket.sent).map((payload) => payload.kind)).toEqual(["pause"]);
    });

    it("still resumes from an active snooze", async () => {
        const socket = await boot();
        socket.__deliver({
            type: "QUIET_MODE_STATE",
            payload: { kind: "snooze_15", ends_at_unix_ms: Date.now() + 60_000, duration_minutes: 1 },
            sequence: 2,
            timestamp: Date.now() / 1000,
        });
        await new Promise((r) => setTimeout(r, 10));
        socket.sent.length = 0;

        globalThis.__cortexChrome.commands.onCommand.__dispatch("pause-cortex");
        await new Promise((r) => setTimeout(r, 20));

        expect(toggles(socket.sent).map((payload) => payload.kind)).toEqual(["off"]);
    });
});
