/**
 * F52: synthesise close actions per-tab_index dedup.
 *
 * - When suggested_actions already covers a tab_index, do not synthesise.
 * - Synthesise only for tab indices not yet covered.
 * - Empty tab recs short-circuit.
 */

import { describe, expect, it } from "vitest";
import { synthesizeActions } from "../popup";

describe("F52 synthesizeActions dedup", () => {
    it("drops the synthesised action when tab_index is already covered", () => {
        const existing = [
            {
                action_id: "real_1",
                action_type: "close_tab",
                tab_index: 3,
                category: "recommended",
                label: "Close existing",
            },
        ];
        const tabRecs = {
            tabs: [
                { tab_index: 3, action: "close", tab_title: "Dup tab" },
            ],
            summary: "",
        };
        const out = synthesizeActions(existing, tabRecs);
        const closeCount = out.filter(
            (a) =>
                (a.action_type === "close_tab" || a.action_type === "bookmark_and_close") &&
                a.tab_index === 3,
        ).length;
        expect(closeCount).toBe(1);
        expect(out).toEqual(existing);
    });

    it("synthesises only for uncovered tab indices", () => {
        const existing = [
            {
                action_id: "real_1",
                action_type: "close_tab",
                tab_index: 1,
                category: "recommended",
                label: "Close 1",
            },
        ];
        const tabRecs = {
            tabs: [
                { tab_index: 1, action: "close", tab_title: "Already covered" },
                { tab_index: 2, action: "close", tab_title: "Uncovered" },
                { tab_index: 5, action: "bookmark_and_close", tab_title: "Also new" },
            ],
            summary: "",
        };
        const out = synthesizeActions(existing, tabRecs);
        const tabsRepresented = new Set<number>();
        for (const a of out) {
            if (
                a.action_type === "close_tab" ||
                a.action_type === "bookmark_and_close"
            ) {
                tabsRepresented.add(a.tab_index as number);
            }
        }
        expect(tabsRepresented.has(1)).toBe(true);
        expect(tabsRepresented.has(2)).toBe(true);
        expect(tabsRepresented.has(5)).toBe(true);
        // Each tab_index represented exactly once.
        const closeActionsForOne = out.filter(
            (a) =>
                (a.action_type === "close_tab" || a.action_type === "bookmark_and_close") &&
                a.tab_index === 1,
        );
        expect(closeActionsForOne.length).toBe(1);
        expect(closeActionsForOne[0].action_id).toBe("real_1");
    });

    it("returns input unchanged when tab recs are empty", () => {
        const existing = [{ action_id: "a", action_type: "open_url", category: "recommended" }];
        expect(synthesizeActions(existing, null)).toBe(existing);
        expect(synthesizeActions(existing, { tabs: [], summary: "" })).toBe(existing);
    });

    it("synthesises full set when no close-style suggested_action exists", () => {
        const existing = [
            { action_id: "a", action_type: "open_url", category: "recommended" },
        ];
        const tabRecs = {
            tabs: [
                { tab_index: 7, action: "close", tab_title: "X" },
                { tab_index: 8, action: "close", tab_title: "Y" },
            ],
            summary: "",
        };
        const out = synthesizeActions(existing, tabRecs);
        const synthIndices = out
            .filter((a) => a.action_type === "close_tab")
            .map((a) => a.tab_index);
        expect(synthIndices).toEqual([7, 8]);
    });
});
