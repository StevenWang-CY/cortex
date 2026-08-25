import { describe, expect, it } from "vitest";

import {
    canonicalJson,
    sha256Hex,
    verifyActionManifest,
    verifyApplyCommand,
    verifyRestoreCommand,
} from "../lib/intervention-transaction";

const NOW = 1_800_000_000_000;
const CLIENT_BOOT_ID = "11111111-1111-4111-8111-111111111111";
const CLIENT_INSTANCE_ID = "browser_test_instance_0001";
const DAEMON_BOOT_ID = "22222222-2222-4222-8222-222222222222";

function browserAction(overrides: Record<string, unknown> = {}) {
    return {
        action_id: "action-close-1",
        ordinal: 0,
        executor: "browser",
        capability: "open_url",
        parameters_json:
            "{\"suggested_action\":{\"action_id\":\"action-close-1\",\"action_type\":\"open_url\",\"target\":\"https://example.com/reference\"}}",
        reverse_capability: "close_created_tab",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
        ...overrides,
    };
}

function editorAction(overrides: Record<string, unknown> = {}) {
    return {
        action_id: "action-editor-1",
        ordinal: 0,
        executor: "editor",
        capability: "resume_last_active_file",
        parameters_json:
            "{\"suggested_action\":{\"action_id\":\"action-editor-1\",\"action_type\":\"resume_last_active_file\",\"target\":\"/workspace/main.py:8\"}}",
        reverse_capability: "restore_active_file",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
        ...overrides,
    };
}

async function manifestFor(actions = [browserAction()]) {
    const canonical = canonicalJson({
        actions,
        intervention_id: "intervention-1",
        schema_version: "1",
    });
    return {
        schema_version: "1",
        intervention_id: "intervention-1",
        canonical_json: canonical,
        manifest_sha256: await sha256Hex(canonical),
        action_count: actions.length,
        created_at_unix_ms: NOW - 1_000,
        created_at_mono_ns: 5_000_000,
        expires_at_unix_ms: NOW + 299_000,
        ttl_ms: 300_000,
        boot_id: DAEMON_BOOT_ID,
    };
}

async function applyCommand(overrides: Record<string, unknown> = {}) {
    const manifest = await manifestFor();
    return {
        manifest,
        authorization: {
            authorization_id: "authz-1",
            authorization_request_id: "request-1",
            intervention_id: manifest.intervention_id,
            manifest_sha256: manifest.manifest_sha256,
            authorized_action_ids: ["action-close-1"],
            consent_revision: 7,
            authorization_kind: "user_confirmed",
            source_surface: "browser",
            source_client_id: "browser-client",
            requester_boot_id: CLIENT_BOOT_ID,
            issued_at_unix_ms: NOW - 100,
            issued_at_mono_ns: 9_000_000,
            expires_at_unix_ms: NOW + 29_900,
            ttl_ms: 30_000,
            boot_id: DAEMON_BOOT_ID,
            nonce: "a".repeat(32),
        },
        actions: [browserAction()],
        ...overrides,
    };
}

describe("exact intervention transaction validation", () => {
    it("accepts a digest-bound manifest and exact browser authorization", async () => {
        const command = await applyCommand();
        const verified = await verifyApplyCommand(
            command,
            "browser",
            CLIENT_BOOT_ID,
            NOW,
        );
        expect(verified.ownActions.map((action) => action.action_id)).toEqual([
            "action-close-1",
        ]);
    });

    it("rejects canonical body tampering even when the outer IDs are unchanged", async () => {
        const manifest = await manifestFor();
        manifest.canonical_json = manifest.canonical_json.replace(
            "open_url",
            "search_error",
        );
        await expect(verifyActionManifest(manifest, NOW)).rejects.toThrow(
            "manifest digest mismatch",
        );
    });

    it("rejects expired manifests and authorizations", async () => {
        const manifest = await manifestFor();
        await expect(
            verifyActionManifest(manifest, manifest.expires_at_unix_ms),
        ).rejects.toThrow("manifest expired");

        const command = await applyCommand();
        await expect(
            verifyApplyCommand(
                command,
                "browser",
                CLIENT_BOOT_ID,
                command.authorization.expires_at_unix_ms,
            ),
        ).rejects.toThrow("authorization expired");
    });

    it("accepts a browser-approved editor action on the selected editor worker", async () => {
        const action = editorAction();
        const manifest = await manifestFor([action]);
        const base = await applyCommand();
        const command = {
            manifest,
            authorization: {
                ...base.authorization,
                intervention_id: manifest.intervention_id,
                manifest_sha256: manifest.manifest_sha256,
                authorized_action_ids: [action.action_id],
                source_surface: "browser",
                requester_boot_id: CLIENT_BOOT_ID,
            },
            actions: [action],
        };
        const verified = await verifyApplyCommand(
            command,
            "editor",
            "33333333-3333-4333-8333-333333333333",
            NOW,
        );
        expect(verified.ownActions.map((item) => item.action_id)).toEqual([
            "action-editor-1",
        ]);
    });

    it("rejects a manifest with a gap in its action ordinals", async () => {
        const manifest = await manifestFor([
            browserAction({ ordinal: 1 }),
        ]);
        await expect(verifyActionManifest(manifest, NOW)).rejects.toThrow(
            "contiguous from zero",
        );
    });

    it("binds browser-origin approval to the requesting worker boot", async () => {
        const command = await applyCommand();
        await expect(
            verifyApplyCommand(
                command,
                "browser",
                "33333333-3333-4333-8333-333333333333",
                NOW,
            ),
        ).rejects.toThrow("another browser instance");
    });

    it("binds browser-origin approval to the durable browser profile", async () => {
        const command = await applyCommand();
        command.authorization.source_client_id = CLIENT_INSTANCE_ID;
        await expect(verifyApplyCommand(
            command,
            "browser",
            CLIENT_BOOT_ID,
            NOW,
            CLIENT_INSTANCE_ID,
        )).resolves.toMatchObject({ ownActions: expect.any(Array) });

        command.authorization.source_client_id = "browser_other_profile_0002";
        await expect(verifyApplyCommand(
            command,
            "browser",
            CLIENT_BOOT_ID,
            NOW,
            CLIENT_INSTANCE_ID,
        )).rejects.toThrow("another stable client instance");
    });

    it("rejects action-set widening and action mutation", async () => {
        const command = await applyCommand();
        command.authorization.authorized_action_ids = [
            "action-close-1",
            "undeclared-action",
        ];
        await expect(
            verifyApplyCommand(command, "browser", CLIENT_BOOT_ID, NOW),
        ).rejects.toThrow("apply action set differs");

        const mutated = await applyCommand();
        mutated.actions[0] = browserAction({ capability: "search_error" });
        await expect(
            verifyApplyCommand(mutated, "browser", CLIENT_BOOT_ID, NOW),
        ).rejects.toThrow("apply action differs from immutable manifest");
    });

    it("fails closed for capabilities absent from the local bounded adapter", async () => {
        const action = browserAction({ capability: "arbitrary_chrome_call" });
        const manifest = await manifestFor([action]);
        await expect(verifyActionManifest(manifest, NOW)).rejects.toThrow(
            "not locally supported",
        );
    });

    it("accepts Python-canonical finite exponent metadata as opaque digest data", async () => {
        const action = browserAction({ parameters_json: "{\"threshold\":1e-07}" });
        const manifest = await manifestFor([action]);
        await expect(verifyActionManifest(manifest, NOW)).resolves.toMatchObject({
            manifest: { intervention_id: "intervention-1" },
        });
    });

    it("routes only owned exact inverse actions", () => {
        const restored = verifyRestoreCommand({
            restore_id: "restore-1",
            intervention_id: "intervention-1",
            manifest_sha256: "f".repeat(64),
            reason: "user_undo",
            requested_at_unix_ms: NOW,
            requested_at_mono_ns: 10_000_000,
            boot_id: DAEMON_BOOT_ID,
            actions: [{
                action_id: "action-close-1",
                executor: "browser",
                reverse_capability: "close_created_tab",
                inverse_payload_json: "{\"tabId\":9,\"url\":\"https://example.com/reference\"}",
                original_authorization_id: "authz-1",
                owner_client_instance_id: CLIENT_INSTANCE_ID,
            }],
        }, "browser", CLIENT_INSTANCE_ID);
        expect(restored.ownActions).toHaveLength(1);

        expect(() => verifyRestoreCommand(
            restored.command,
            "browser",
            "browser_other_profile_0002",
        )).toThrow("this client instance");

        expect(() => verifyRestoreCommand({
            ...restored.command,
            reason: "invented_reason",
        }, "browser", CLIENT_INSTANCE_ID)).toThrow("restore reason is invalid");
    });
});
