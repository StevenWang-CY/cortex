/**
 * WYSIWYG regression coverage for the former F52 synthesis path.
 *
 * Tab recommendations are presentation-only. They must never be promoted to
 * executable close actions, and a visible action only receives authority when
 * its exact JSON presentation is committed by the verified manifest.
 */

import { describe, expect, it } from "vitest";
import {
    canonicalJson,
    sha256Hex,
    verifiedPresentedActionIds,
} from "../lib/intervention-transaction";

const NOW = 1_800_000_000_000;

async function planFor(suggestion: Record<string, unknown>) {
    const manifestAction = {
        action_id: suggestion.action_id,
        ordinal: 0,
        executor: "browser",
        capability: suggestion.action_type,
        parameters_json: canonicalJson({ suggested_action: suggestion }),
        reverse_capability: "close_created_tab",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
    };
    const body = canonicalJson({
        actions: [manifestAction],
        intervention_id: "iv-wysiwyg",
        schema_version: "1",
    });
    return {
        intervention_id: "iv-wysiwyg",
        suggested_actions: [suggestion],
        tab_recommendations: null,
        action_manifest: {
            schema_version: "1",
            intervention_id: "iv-wysiwyg",
            canonical_json: body,
            manifest_sha256: await sha256Hex(body),
            action_count: 1,
            created_at_unix_ms: NOW - 1_000,
            created_at_mono_ns: 1_000_000,
            expires_at_unix_ms: NOW + 299_000,
            ttl_ms: 300_000,
            boot_id: "11111111-1111-4111-8111-111111111111",
        },
    };
}

describe("verified intervention presentation", () => {
    it("enables an exact manifest-bound safe action", async () => {
        const suggestion = {
            action_id: "action-open",
            action_type: "open_url",
            target: "https://example.com/reference",
            label: "Open reference",
            reason: "Directly relevant",
            category: "recommended",
            reversible: true,
            metadata: {},
        };
        const plan = await planFor(suggestion);
        await expect(
            verifiedPresentedActionIds(plan, "browser", NOW),
        ).resolves.toEqual(["action-open"]);
    });

    it("does not synthesize authority from tab recommendations", async () => {
        const canonical = canonicalJson({
            actions: [],
            intervention_id: "iv-manual-tabs",
            schema_version: "1",
        });
        const plan = {
            intervention_id: "iv-manual-tabs",
            suggested_actions: [],
            tab_recommendations: {
                tabs: [{ tab_index: 2, action: "close", tab_title: "Noise" }],
                summary: "Review one tab",
            },
            action_manifest: {
                schema_version: "1",
                intervention_id: "iv-manual-tabs",
                canonical_json: canonical,
                action_count: 0,
                created_at_unix_ms: NOW - 1_000,
                created_at_mono_ns: 1_000_000,
                expires_at_unix_ms: NOW + 299_000,
                ttl_ms: 300_000,
                boot_id: "11111111-1111-4111-8111-111111111111",
                manifest_sha256: await sha256Hex(canonical),
            },
        };
        await expect(
            verifiedPresentedActionIds(plan, "browser", NOW),
        ).resolves.toEqual([]);
    });

    it("withholds the affordance when visible copy differs from the manifest", async () => {
        const committed = {
            action_id: "action-open",
            action_type: "open_url",
            target: "https://example.com/reference",
            label: "Open reference",
            reason: "Directly relevant",
            category: "recommended",
            reversible: true,
            metadata: {},
        };
        const plan = await planFor(committed);
        plan.suggested_actions = [{ ...committed, label: "Open something else" }];
        await expect(
            verifiedPresentedActionIds(plan, "browser", NOW),
        ).resolves.toEqual([]);
    });
});
