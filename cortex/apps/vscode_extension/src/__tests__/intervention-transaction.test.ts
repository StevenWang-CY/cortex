import {
    canonicalJson,
    sha256Hex,
    verifyApplyCommand,
    verifyRestoreCommand,
} from "../intervention-transaction";

const NOW = 1_800_000_000_000;
const EDITOR_BOOT = "11111111-1111-4111-8111-111111111111";
const EDITOR_INSTANCE = "vscode_test_instance_0001";

function editorAction(overrides: Record<string, unknown> = {}) {
    return {
        action_id: "resume-1",
        ordinal: 0,
        executor: "editor",
        capability: "resume_last_active_file",
        parameters_json: canonicalJson({
            suggested_action: {
                action_id: "resume-1",
                action_type: "resume_last_active_file",
                target: "/workspace/main.py:8",
            },
        }),
        reverse_capability: "restore_active_file",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
        ...overrides,
    };
}

function command() {
    const action = editorAction();
    const canonical = canonicalJson({
        actions: [action],
        intervention_id: "intervention-editor",
        schema_version: "1",
    });
    const digest = sha256Hex(canonical);
    return {
        manifest: {
            schema_version: "1",
            intervention_id: "intervention-editor",
            canonical_json: canonical,
            manifest_sha256: digest,
            action_count: 1,
            created_at_unix_ms: NOW - 1_000,
            created_at_mono_ns: 1_000_000,
            expires_at_unix_ms: NOW + 299_000,
            ttl_ms: 300_000,
            boot_id: "22222222-2222-4222-8222-222222222222",
        },
        authorization: {
            authorization_id: "authz-editor",
            authorization_request_id: "request-editor",
            intervention_id: "intervention-editor",
            manifest_sha256: digest,
            authorized_action_ids: ["resume-1"],
            consent_revision: 4,
            authorization_kind: "user_confirmed",
            source_surface: "vscode",
            source_client_id: "editor-client",
            requester_boot_id: EDITOR_BOOT,
            issued_at_unix_ms: NOW - 100,
            issued_at_mono_ns: 2_000_000,
            expires_at_unix_ms: NOW + 29_900,
            ttl_ms: 30_000,
            boot_id: "22222222-2222-4222-8222-222222222222",
            nonce: "a".repeat(32),
        },
        actions: [action],
    };
}

describe("editor transaction validation", () => {
    it("accepts only an exact, current-worker authorization", () => {
        expect(verifyApplyCommand(command(), EDITOR_BOOT, NOW).ownActions).toHaveLength(1);
        expect(() => verifyApplyCommand(
            command(),
            "33333333-3333-4333-8333-333333333333",
            NOW,
        )).toThrow("another editor instance");
    });

    it("accepts a browser-approved editor capability on the selected editor", () => {
        const approvedInBrowser = command();
        approvedInBrowser.authorization.source_surface = "browser";
        approvedInBrowser.authorization.requester_boot_id =
            "33333333-3333-4333-8333-333333333333";
        expect(
            verifyApplyCommand(approvedInBrowser, EDITOR_BOOT, NOW).ownActions,
        ).toHaveLength(1);
    });

    it("binds editor-origin approval to the durable VS Code instance", () => {
        const exact = command();
        exact.authorization.source_client_id = EDITOR_INSTANCE;
        expect(verifyApplyCommand(
            exact,
            EDITOR_BOOT,
            NOW,
            EDITOR_INSTANCE,
        ).ownActions).toHaveLength(1);

        exact.authorization.source_client_id = "vscode_other_instance_0002";
        expect(() => verifyApplyCommand(
            exact,
            EDITOR_BOOT,
            NOW,
            EDITOR_INSTANCE,
        )).toThrow("another stable editor instance");
    });

    it("expires manifest and authorization at their exact boundaries", () => {
        const manifestExpired = command();
        expect(() => verifyApplyCommand(
            manifestExpired,
            EDITOR_BOOT,
            manifestExpired.manifest.expires_at_unix_ms,
        )).toThrow("manifest expired");

        const authorizationExpired = command();
        expect(() => verifyApplyCommand(
            authorizationExpired,
            EDITOR_BOOT,
            authorizationExpired.authorization.expires_at_unix_ms,
        )).toThrow("authorization expired");
    });

    it("rejects unsupported folding because ownership cannot be proven", () => {
        const altered = command();
        const fold = editorAction({ capability: "fold_except_current" });
        const canonical = canonicalJson({
            actions: [fold],
            intervention_id: "intervention-editor",
            schema_version: "1",
        });
        altered.manifest.canonical_json = canonical;
        altered.manifest.manifest_sha256 = sha256Hex(canonical);
        altered.actions = [fold];
        altered.authorization.manifest_sha256 = altered.manifest.manifest_sha256;
        expect(() => verifyApplyCommand(altered, EDITOR_BOOT, NOW)).toThrow(
            "not locally supported",
        );
    });

    it("validates restore ownership shape and reason", () => {
        const restored = verifyRestoreCommand({
            restore_id: "restore-editor",
            intervention_id: "intervention-editor",
            manifest_sha256: "f".repeat(64),
            reason: "user_undo",
            requested_at_unix_ms: NOW,
            requested_at_mono_ns: 4_000_000,
            boot_id: EDITOR_BOOT,
            actions: [{
                action_id: "resume-1",
                executor: "editor",
                reverse_capability: "restore_active_file",
                inverse_payload_json: "{}",
                original_authorization_id: "authz-editor",
                owner_client_instance_id: EDITOR_INSTANCE,
            }],
        }, EDITOR_INSTANCE);
        expect(restored.ownActions).toHaveLength(1);
        expect(() => verifyRestoreCommand(
            restored.command,
            "vscode_other_instance_0002",
        )).toThrow("this client instance");
        expect(() => verifyRestoreCommand({
            ...restored.command,
            reason: "unsafe",
        }, EDITOR_INSTANCE)).toThrow("restore reason is invalid");
    });
});
