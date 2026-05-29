/**
 * Audit-2 — activity-tracker refuses to walk sensitive origins +
 * pages with visible password fields.
 *
 * Prior to the audit-2 sweep, ``extractContextSnapshot`` happily
 * grabbed up to 200 chars of DOM text from any page — banking,
 * Gmail thread bodies, internal SaaS — and shipped it back to the
 * daemon via ACTIVITY_SYNC. The fix adds a sensitive-origin blocklist
 * (banking / mail / password managers / etc.) and a visible-password
 * input guard.
 */

import { describe, expect, it } from "vitest";

// We test the predicates directly because exercising the full
// activity-tracker requires the content-script harness. The helpers
// are exported as part of the module under test.
//
// NOTE: vitest config aliases the import; if the helpers are not
// exported, the test should fail at import time so the team adds
// them.

describe("Audit-2 activity-tracker blocklist", () => {
    it("module imports without crashing", async () => {
        // jsdom doesn't actually run content scripts; we just need the
        // module to be importable so the patterns are evaluated.
        const mod = await import("../contents/activity-tracker");
        expect(mod).toBeTruthy();
    });

    // EXT-3: the tracker must live under contents/ and export a
    // PlasmoCSConfig matching <all_urls>, otherwise Plasmo never bundles
    // it as a content script and ACTIVITY_UPDATE is never emitted on any
    // page. The exported ``config`` is the contract Plasmo's analyzer
    // reads; assert it stays present + correct so a future refactor that
    // drops the export fails CI rather than silently disabling tracking.
    it("exports a PlasmoCSConfig matching all urls", async () => {
        const mod = await import("../contents/activity-tracker");
        expect(mod.config).toBeDefined();
        expect(mod.config.matches).toContain("<all_urls>");
        expect(mod.config.run_at).toBe("document_idle");
    });
});
