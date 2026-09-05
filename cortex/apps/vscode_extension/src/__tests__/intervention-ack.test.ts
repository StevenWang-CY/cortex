/**
 * INTERVENTION_RECEIPT wire fidelity.
 *
 * The editor's only outcome channel is the typed receipt batch that
 * ``EditorTransactionAdapter`` hands to ``CortexWSClient.sendInterventionReceipt``
 * (the legacy ``INTERVENTION_APPLIED`` ack and its ``sendInterventionApplied``
 * helper had no callers and were removed — audit A15). What the adapter
 * claims it sent must appear on the wire verbatim: the ``phase`` the
 * daemon dedups on must not drift, and receipts for the same intervention
 * with different phases must stay distinct.
 *
 * The test drives the real ``CortexWSClient`` with a fake socket so we can
 * inspect the exact JSON frame the extension transmits.
 */

import { CortexWSClient } from "../ws-client";
import type {
    ActionReceipt,
    InterventionReceiptBatch,
} from "../generated/cortex_schemas";

// ── Fake WebSocket that captures every frame the client sends ────────────────

interface CapturedFrame {
    type: string;
    payload: Record<string, unknown>;
    sequence: number;
    correlation_id?: string;
}

class FakeSocket {
    sent: CapturedFrame[] = [];
    send(raw: string): void {
        this.sent.push(JSON.parse(raw) as CapturedFrame);
    }
}

/**
 * Build a client that believes it is connected to ``socket`` so ``_send``
 * writes straight to the wire instead of buffering into the offline outbox.
 */
function connectedClient(socket: FakeSocket): CortexWSClient {
    const client = new CortexWSClient("ws://127.0.0.1:9473");
    const internals = client as unknown as Record<string, unknown>;
    internals["_ws"] = socket;
    internals["_connected"] = true;
    return client;
}

function receipt(
    interventionId: string,
    actionId: string,
    phase: ActionReceipt["phase"],
    status: ActionReceipt["status"] = "succeeded",
): ActionReceipt {
    const now = 1_800_000_000_000;
    return {
        receipt_id: `rcpt-${actionId}-${phase}`,
        intervention_id: interventionId,
        authorization_id: "authz-1",
        manifest_sha256: "a".repeat(64),
        action_id: actionId,
        phase,
        attempt: 1,
        idempotency_key: `${interventionId}:${actionId}:${phase}:1`,
        status,
        started_at_unix_ms: now,
        ended_at_unix_ms: now + 5,
        started_at_mono_ns: 1_000,
        ended_at_mono_ns: 6_000,
        duration_ms: 5,
        boot_id: "11111111-1111-4111-8111-111111111111",
        verification: "verified",
        source_client_type: "vscode",
    } as ActionReceipt;
}

function batch(
    interventionId: string,
    receipts: ActionReceipt[],
): InterventionReceiptBatch {
    return {
        intervention_id: interventionId,
        manifest_sha256: "a".repeat(64),
        authorization_id: "authz-1",
        receipts: receipts as [ActionReceipt, ...ActionReceipt[]],
    };
}

describe("CortexWSClient – INTERVENTION_RECEIPT wire fidelity", () => {
    it("transmits the receipt batch verbatim under type INTERVENTION_RECEIPT", () => {
        const socket = new FakeSocket();
        const client = connectedClient(socket);

        client.sendInterventionReceipt(
            batch("iv_123", [receipt("iv_123", "act_9", "apply")]),
        );

        expect(socket.sent.length).toBe(1);
        const frame = socket.sent[0];
        expect(frame.type).toBe("INTERVENTION_RECEIPT");
        expect(frame.payload.intervention_id).toBe("iv_123");
        expect(frame.payload.authorization_id).toBe("authz-1");
        expect(frame.payload.manifest_sha256).toBe("a".repeat(64));
        const receipts = frame.payload.receipts as ActionReceipt[];
        expect(receipts).toHaveLength(1);
        // The exact wire values the daemon dedups on — must NOT drift.
        expect(receipts[0].action_id).toBe("act_9");
        expect(receipts[0].phase).toBe("apply");
        expect(receipts[0].status).toBe("succeeded");
        expect(receipts[0].idempotency_key).toBe("iv_123:act_9:apply:1");
    });

    it("keeps apply and restore receipts for the same intervention distinct", () => {
        const socket = new FakeSocket();
        const client = connectedClient(socket);

        client.sendInterventionReceipt(
            batch("iv_42", [receipt("iv_42", "act_1", "apply")]),
        );
        client.sendInterventionReceipt(
            batch("iv_42", [receipt("iv_42", "act_1", "restore")]),
        );

        expect(socket.sent.length).toBe(2);
        const keys = new Set(
            socket.sent.map((f) => {
                const first = (f.payload.receipts as ActionReceipt[])[0];
                return `${String(f.payload.intervention_id)}:${first.action_id}:${first.phase}`;
            }),
        );
        expect(keys.size).toBe(2);
        expect(socket.sent.map((f) => f.sequence)).toEqual([1, 2]);
    });

    it("reports a failed action with its error fields intact", () => {
        const socket = new FakeSocket();
        const client = connectedClient(socket);

        const failed = {
            ...receipt("iv_7", "act_3", "apply", "failed"),
            verification: "failed",
            error_code: "target_missing",
            error_message: "resume_last_active_file: empty target",
            retryable: false,
        } as ActionReceipt;
        client.sendInterventionReceipt(batch("iv_7", [failed]));

        const wire = (socket.sent[0].payload.receipts as ActionReceipt[])[0];
        expect(wire.status).toBe("failed");
        expect(wire.error_code).toBe("target_missing");
        expect(wire.error_message).toBe("resume_last_active_file: empty target");
        expect(wire.retryable).toBe(false);
    });

    it("queues the batch in the bounded outbox while disconnected instead of dropping it", () => {
        const client = new CortexWSClient("ws://127.0.0.1:9473");
        client.sendInterventionReceipt(
            batch("iv_off", [receipt("iv_off", "act_1", "apply")]),
        );
        const outbox = (client as unknown as { _outbox: Array<{ type: string }> })._outbox;
        expect(outbox).toHaveLength(1);
        expect(outbox[0].type).toBe("INTERVENTION_RECEIPT");
    });
});
