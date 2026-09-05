import { describe, expect, it } from "vitest";
import {
    applyStatusLabel,
    humaniseApplyReason,
    isApplyOutcome,
    outcomeIsUndoable,
    reduceApplyResults,
} from "../lib/apply-state";
import { BadgeState } from "../lib/badge-state";

describe("apply state machine", () => {
    it("reduces all-success results to applied", () => {
        const outcome = reduceApplyResults([
            { action_id: "a", success: true, message: "ok", reversible: true },
            { action_id: "b", success: true, message: "ok", reversible: true },
        ]);
        expect(outcome).toEqual({ phase: "applied", applied: 2, total: 2, reason: null });
        expect(applyStatusLabel(outcome)).toBe("Applied");
        expect(outcomeIsUndoable(outcome)).toBe(true);
    });

    it("reduces mixed results to partial with a consumer reason", () => {
        const outcome = reduceApplyResults([
            { action_id: "a", success: true, message: "ok", reversible: true },
            { action_id: "b", success: false, message: "Authorization timed out before execution", reversible: false },
            { action_id: "c", success: true, message: "ok", reversible: true },
        ]);
        expect(outcome.phase).toBe("partial");
        expect(applyStatusLabel(outcome)).toBe("2 of 3 applied");
        expect(outcome.reason).toBe("Cortex didn't respond in time");
    });

    it("never treats an error object, an empty list, or garbage as success", () => {
        const fromError = reduceApplyResults({ success: false, message: "Intervention is no longer active" });
        expect(fromError.phase).toBe("failed");
        expect(applyStatusLabel(fromError)).toBe("Nothing changed — this suggestion has expired");
        expect(outcomeIsUndoable(fromError)).toBe(false);
        expect(reduceApplyResults([]).phase).toBe("failed");
        expect(reduceApplyResults(undefined, "Cortex didn't respond").reason).toBe("Cortex didn't respond");
        expect(reduceApplyResults("nonsense").phase).toBe("failed");
    });

    it("translates executor messages into calm language without codes", () => {
        expect(humaniseApplyReason("Action unavailable in suggest-only mode")).toBe("workspace changes are off");
        expect(humaniseApplyReason("Displayed action differs from the immutable manifest")).toBe("the suggestion changed before it could run");
        expect(humaniseApplyReason("Error: Tab navigated away.")).toBe("tab navigated away");
        expect(humaniseApplyReason("")).toBe("something went wrong");
    });

    it("guards the wire shape", () => {
        expect(isApplyOutcome({ phase: "applied", applied: 1, total: 1, reason: null })).toBe(true);
        expect(isApplyOutcome({ phase: "done", applied: 1, total: 1, reason: null })).toBe(false);
        expect(isApplyOutcome(null)).toBe(false);
    });
});

describe("badge priority", () => {
    it("lets a pending intervention outrank an unread recap and reveals the recap afterwards", () => {
        const badge = new BadgeState();
        expect(badge.setRecap(true)).toBe("✓");
        expect(badge.setIntervention(true)).toBe("1");
        expect(badge.setRecap(false)).toBe("1");
        expect(badge.setRecap(true)).toBe("1");
        expect(badge.setIntervention(false)).toBe("✓");
        expect(badge.setRecap(false)).toBe("");
    });
});
