/**
 * F17 — browser-extension drops reordered / stale WS frames.
 *
 * The daemon's WS server increments WSMessage.sequence once per
 * outbound message; the extension maintains a per-type last-applied
 * counter and ignores any frame whose sequence is not strictly
 * greater. The drop happens before the per-type switch, so neither
 * STATE_UPDATE nor INTERVENTION_TRIGGER can clobber the active UI
 * state with a stale frame.
 *
 * Tests poke ``_acceptSequencedFrame`` directly (the test export wired
 * by background.ts) so we don't need to instantiate a real WebSocket
 * or drive the full ``handleMessage`` plumbing — the drop predicate
 * is the F17 contract.
 *
 * Note: background.ts has top-level side effects that need the
 * ``chrome.*`` fake installed first. We rely on the global setup
 * (``test/setup.ts``) installing the fake in ``beforeEach``, and we
 * import background.ts dynamically inside each test so the fake is
 * already present at module-evaluation time.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WSMessage } from "../types/generated/cortex_schemas";

function frame(type: string, seq: number): WSMessage {
    return {
        type: type as WSMessage["type"],
        payload: {},
        timestamp: 0,
        sequence: seq,
        correlation_id: null,
        target_client_types: null,
        source_client_type: null,
    } as WSMessage;
}

describe("F17 — extension drops stale WS frames", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("accepts a strictly greater sequence and updates the tracker", async () => {
        const bg = await import("../background");
        bg._resetLastSeqByType();
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 10))).toBe(true);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(10);
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 11))).toBe(true);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(11);
    });

    it("rejects a sequence that is not strictly greater", async () => {
        const bg = await import("../background");
        bg._resetLastSeqByType();
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 10))).toBe(true);
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 9))).toBe(false);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(10);
    });

    it("rejects duplicate sequences", async () => {
        const bg = await import("../background");
        bg._resetLastSeqByType();
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 5))).toBe(true);
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 5))).toBe(false);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(5);
    });

    it("tracks per-type counters independently", async () => {
        const bg = await import("../background");
        bg._resetLastSeqByType();
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 100))).toBe(true);
        // Intervention seq=1 must NOT be rejected just because state
        // already saw seq=100.
        expect(bg._acceptSequencedFrame(frame("INTERVENTION_TRIGGER", 1))).toBe(true);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(100);
        expect(bg._getLastSeq("INTERVENTION_TRIGGER")).toBe(1);
    });

    it("bypasses the check for sequence=0 (older daemons / unsequenced types)", async () => {
        const bg = await import("../background");
        bg._resetLastSeqByType();
        // First seq=0 frame: applied, tracker unchanged (still 0).
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 0))).toBe(true);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(0);
        // Subsequent seq=0 also applies — the contract is "bypass on
        // sequence<=0", not "lock the tracker".
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 0))).toBe(true);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(0);
    });

    it("resets via _resetLastSeqByType so a daemon-restart seq=1 wins", async () => {
        const bg = await import("../background");
        bg._resetLastSeqByType();
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 500))).toBe(true);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(500);

        // Daemon restart: tracker is reset by the WS onopen path.
        bg._resetLastSeqByType();
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(0);

        // The new daemon's first frame at seq=1 must be applied.
        expect(bg._acceptSequencedFrame(frame("STATE_UPDATE", 1))).toBe(true);
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(1);
    });

    it("treats a frame with missing type as unsequenced", async () => {
        const bg = await import("../background");
        bg._resetLastSeqByType();
        const malformed = { ...frame("STATE_UPDATE", 10), type: "" } as unknown as WSMessage;
        expect(bg._acceptSequencedFrame(malformed)).toBe(true);
        // Tracker for the real "STATE_UPDATE" type is unchanged.
        expect(bg._getLastSeq("STATE_UPDATE")).toBe(0);
    });
});
