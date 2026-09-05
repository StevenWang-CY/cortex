import { describe, expect, it } from "vitest";

import {
    buildActivitySyncPayload,
    canonicalizeActivityUrl,
} from "../lib/activity-store";
import {
    createFocusSession,
    focusSessionSnapshot,
    isDistractionForSession,
    resolveFocusPreset,
    updateFocusSessionState,
} from "../lib/focus-session";
import { BrowserSessionStore } from "../lib/persisted-session";
import {
    FrameReplayGuard,
    ParseErrorWindow,
    ReconnectBackoff,
    SerialCommandQueue,
} from "../lib/daemon-connection";
import { InterventionPresentationState } from "../lib/intervention-presentation";
import { normalizeTabs } from "../lib/context-collector";
import {
    CapabilityExecutor,
    UnsupportedCapabilityError,
} from "../lib/capability-executor";
import type { ActivityRecord } from "../lib/activity-privacy";
import {
    connectivityViewModel,
    supportStateViewModel,
} from "../lib/popup-view-model";
import { TabActivationTelemetry } from "../lib/browser-telemetry";

function activity(id: string, visited: number): ActivityRecord {
    return {
        content_id: id,
        platform: "docs",
        content_type: "general",
        title: id,
        url: `https://example.com/${id}`,
        favicon_url: "",
        position: { type: "general", scroll_pct: 40 },
        content_duration_s: 0,
        duration_spent_s: 12,
        session_duration_s: 12,
        first_visited: visited - 1,
        last_visited: visited,
        context_snapshot: "",
        topic_tags: [],
        completion_pct: 40,
        max_completion_pct: 40,
        cognitive_state: "",
        visit_count: 1,
        dismissed: false,
        is_playlist: false,
        playlist_id: "",
        playlist_index: -1,
        related_tabs: [],
    };
}

describe("browser bounded coordinators", () => {
    it("updates focus duration only from estimated FLOW/RECOVERY intervals", () => {
        const session = createFocusSession("ship api", 1_000);
        updateFocusSessionState(
            session,
            { state: "FLOW", status: "estimated" },
            2_000,
        );
        updateFocusSessionState(
            session,
            { state: "FLOW", status: "insufficient_evidence" },
            3_000,
        );

        expect(session.totalFocusMs).toBe(1_000);
        expect(focusSessionSnapshot(session, 3_000)).toMatchObject({
            elapsedMs: 2_000,
            focusMs: 1_000,
            focusPct: 50,
        });
    });

    it("keeps goal-relevant video available while blocking preset domains", () => {
        const session = createFocusSession("learn Rust ownership", 1_000);
        expect(isDistractionForSession({
            url: "https://youtube.com/watch?v=1",
            title: "Rust ownership explained",
            session,
        })).toBe(false);
        expect(isDistractionForSession({
            url: "https://reddit.com/r/rust",
            title: "Rust ownership",
            session,
            presetPatterns: resolveFocusPreset("developer"),
        })).toBe(true);
    });

    it("canonicalizes activity identity and bounds newest-first sync", () => {
        expect(canonicalizeActivityUrl(
            "https://www.example.com/lesson?utm_source=mail#private",
        )).toBe("https://example.com/lesson");
        const records: Record<string, ActivityRecord> = {};
        for (let index = 0; index < 12; index += 1) {
            records[String(index)] = activity(String(index), index);
        }
        const payload = buildActivitySyncPayload(records);
        expect(payload).toHaveLength(10);
        expect(payload[0].content_id).toBe("11");
        expect(payload.at(-1)?.content_id).toBe("2");
    });

    it("minimizes tab context and protects goal-relevant canonical categories", () => {
        const tabs = normalizeTabs([
            {
                id: 7,
                active: true,
                title: "Rust ownership explained — YouTube",
                url: "https://youtube.com/watch?v=secret&utm_source=mail",
            },
            {
                id: 8,
                incognito: true,
                title: "Private",
                url: "https://example.com/private",
            },
        ] as unknown as chrome.tabs.Tab[], {
            focusGoal: "learn Rust ownership",
            lastActivated: new Map([[7, 9_000]]),
            now: 10_000,
        });

        expect(tabs).toHaveLength(1);
        expect(tabs[0]).toMatchObject({
            tab_id: 7,
            url: "https://youtube.com",
            tab_type: "goal_relevant",
            last_activated_ago_seconds: 1,
        });
    });

    it("rejects malformed MV3 session collections at the repository boundary", async () => {
        globalThis.__cortexChrome.storage.session.__reset({
            quietMode: "yes",
            autoFocusArmed: true,
            autoFocusEndsAt: -1,
            autoFocusCustomDomains: ["example.com", 42, "x".repeat(300)],
            dismissedInterventions: [["valid", 10], ["bad", -1]],
            tabLastActivated: [[1, 20]],
        });
        const state = await new BrowserSessionStore().loadSession();

        expect(state.quietMode).toBeUndefined();
        expect(state.autoFocusArmed).toBe(true);
        expect(state.autoFocusEndsAt).toBeUndefined();
        expect(state.autoFocusCustomDomains).toEqual(["example.com"]);
        expect(state.dismissedInterventions).toBeUndefined();
        expect(state.tabLastActivated).toEqual([[1, 20]]);
    });

    it("owns reconnect, replay, parse-error, and command ordering state", async () => {
        const backoff = new ReconnectBackoff(10, 40);
        expect([
            backoff.takeAndAdvance(),
            backoff.takeAndAdvance(),
            backoff.takeAndAdvance(),
            backoff.takeAndAdvance(),
        ]).toEqual([10, 20, 40, 40]);
        backoff.reset();
        expect(backoff.current).toBe(10);

        const replay = new FrameReplayGuard(2);
        expect(replay.accept({ type: "STATE", sequence: 2, event_id: "a" })).toBe(true);
        expect(replay.accept({ type: "STATE", sequence: 1, event_id: "b" })).toBe(false);
        expect(replay.accept({ type: "OTHER", sequence: 1, event_id: "a" })).toBe(false);

        const errors = new ParseErrorWindow(100, 2);
        expect(errors.record(1_000)).toBe(false);
        expect(errors.record(1_050)).toBe(true);
        errors.reset();
        expect(errors.count).toBe(0);

        const order: number[] = [];
        const queue = new SerialCommandQueue(() => undefined);
        const first = queue.enqueue(async () => {
            await Promise.resolve();
            order.push(1);
        });
        const second = queue.enqueue(async () => {
            order.push(2);
        });
        await Promise.all([first, second]);
        expect(order).toEqual([1, 2]);
    });

    it("owns presentation swaps, bounded cooldowns, and typed capabilities", async () => {
        const presentation = new InterventionPresentationState();
        presentation.mount({ intervention_id: "one" }, "cid-one", 100);
        presentation.mount({ intervention_id: "two" }, "cid-two", 200);
        expect(presentation.active?.plan.intervention_id).toBe("two");
        presentation.dismiss("two", "https://example.com/path", 300);
        expect(presentation.suppression("two", null, 301)).toBe("intervention");
        expect(presentation.suppression("new", "https://example.com", 301)).toBe("url");
        presentation.configureCooldowns({ interventionMs: 0, urlMs: 0 });
        expect(presentation.suppression("two", "https://example.com", 302)).toBeNull();

        type Action = { action_type: "open" | "close"; id: string };
        const executor = new CapabilityExecutor<
            Action,
            { suffix: string },
            string
        >({
            open: async (action, context) => `open:${action.id}:${context.suffix}`,
            close: async (action, context) => `close:${action.id}:${context.suffix}`,
        });
        await expect(executor.execute(
            { action_type: "open", id: "1" },
            { suffix: "ok" },
        )).resolves.toBe("open:1:ok");
        await expect(executor.execute(
            { action_type: "future", id: "2" } as unknown as Action,
            { suffix: "no" },
        )).rejects.toBeInstanceOf(UnsupportedCapabilityError);
    });

    it("derives popup copy from transport state outside the React view", () => {
        const handshake = connectivityViewModel({
            state: "handshake_failed",
            launching: false,
            launchError: false,
            launchStatus: "",
            expectedVersion: "2.0.0",
            daemonVersion: "2.0.0",
            handshakeError: "token rejected",
        });
        expect(handshake).toMatchObject({
            title: "Cortex couldn't verify this browser",
            action: "handshake",
            disabled: false,
        });
        // Raw codes are diagnostics; the body is guidance the user can act on.
        expect(handshake.body).not.toContain("token rejected");
        expect(handshake.body).toContain("Connect Extensions");
        expect(supportStateViewModel({
            state: "FLOW",
            status: "insufficient_evidence",
            confidence: 0,
            scores: {},
            signal_quality: {},
            dwell_seconds: 0,
            capture: { frames_flowing: false, stale: true },
        }, true, { FLOW: "Steady activity" })).toMatchObject({
            stateKey: "UNKNOWN",
            label: "Not enough evidence",
            captureStale: true,
        });
    });

    it("owns tab-activation telemetry and rejects invalid identifiers", () => {
        const telemetry = new TabActivationTelemetry();
        telemetry.recordActivation(4, 1_000);
        telemetry.recordActivation(-1, 2_000);
        telemetry.recordActivation(5, 3_000);
        telemetry.recordRemoval(4);

        expect(telemetry.entries()).toEqual([[5, 3_000]]);
        expect(telemetry.snapshot().get(5)).toBe(3_000);
    });
});
