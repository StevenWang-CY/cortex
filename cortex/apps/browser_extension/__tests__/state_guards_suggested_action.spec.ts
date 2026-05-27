/**
 * P1-13 — isSuggestedAction runtime guard.
 *
 * Verifies that the guard accepts valid SuggestedAction-shaped objects
 * and rejects malformed payloads that should not reach executeAction.
 */

import { describe, expect, it } from "vitest";
import { isSuggestedAction } from "../lib/state-guards";

describe("isSuggestedAction (P1-13)", () => {
    it("returns true for a valid minimal action", () => {
        expect(
            isSuggestedAction({
                action_id: "abc-123",
                action_type: "close_tab",
            }),
        ).toBe(true);
    });

    it("returns true for a full action payload", () => {
        expect(
            isSuggestedAction({
                action_id: "abc-123",
                action_type: "bookmark_and_close",
                target: "tab://42",
                label: "Archive this tab",
                reason: "Tab hasn't been used in 30 minutes",
                category: "workspace",
                reversible: true,
                metadata: { tab_index: 3 },
            }),
        ).toBe(true);
    });

    it("returns false for null", () => {
        expect(isSuggestedAction(null)).toBe(false);
    });

    it("returns false for undefined", () => {
        expect(isSuggestedAction(undefined)).toBe(false);
    });

    it("returns false for a non-object primitive", () => {
        expect(isSuggestedAction("close_tab")).toBe(false);
    });

    it("returns false when action_id is missing", () => {
        expect(
            isSuggestedAction({ action_type: "close_tab" }),
        ).toBe(false);
    });

    it("returns false when action_type is missing", () => {
        expect(
            isSuggestedAction({ action_id: "abc-123" }),
        ).toBe(false);
    });

    it("returns false when action_id is empty string", () => {
        expect(
            isSuggestedAction({ action_id: "", action_type: "close_tab" }),
        ).toBe(false);
    });

    it("returns false when action_type is empty string", () => {
        expect(
            isSuggestedAction({ action_id: "abc-123", action_type: "" }),
        ).toBe(false);
    });

    it("returns false for an empty object", () => {
        expect(isSuggestedAction({})).toBe(false);
    });

    it("returns false when action_id is a number", () => {
        expect(
            isSuggestedAction({ action_id: 42, action_type: "close_tab" }),
        ).toBe(false);
    });
});
