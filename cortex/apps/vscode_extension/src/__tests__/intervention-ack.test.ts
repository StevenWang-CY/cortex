/**
 * P0 (audit-prod, VSCODE slice) — INTERVENTION_APPLIED ack phase fidelity.
 *
 * Two regressions are covered:
 *
 *  1. The EXECUTE_ACTION ``resume_last_active_file`` path acks the daemon
 *     with ``phase: "execute_action"``. Before the fix the ws-client
 *     ``sendInterventionApplied`` signature only accepted
 *     ``"apply" | "restore"`` so the call sites in extension.ts failed to
 *     compile (tsc TS2345) AND, had they been forced through, the daemon's
 *     ``(intervention_id, phase)`` dedup would have collapsed the resume
 *     ack into the UIPlan ``"apply"`` ack for the same intervention — i.e.
 *     the resume outcome went unrecorded ("resume ack unrecorded").
 *
 *  2. The phase the client claims it sent must actually appear on the wire
 *     (no "phase drift" between the public method arg and the JSON frame).
 *
 * The test drives the real ``CortexWSClient`` with a fake socket so we can
 * inspect the exact JSON frame the extension transmits.
 */

import { CortexWSClient } from "../ws-client";

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

describe("CortexWSClient – INTERVENTION_APPLIED ack phase fidelity", () => {
    it('transmits phase "execute_action" verbatim for a resume ack', () => {
        const socket = new FakeSocket();
        const client = connectedClient(socket);

        client.sendInterventionApplied(
            "iv_123",
            "execute_action",
            true,
            ["resume_last_active_file:act_9"],
            [],
        );

        expect(socket.sent.length).toBe(1);
        const frame = socket.sent[0];
        expect(frame.type).toBe("INTERVENTION_APPLIED");
        expect(frame.payload.intervention_id).toBe("iv_123");
        // The exact wire value the daemon dedups on — must NOT drift to
        // "apply" or be coerced away.
        expect(frame.payload.phase).toBe("execute_action");
        expect(frame.payload.success).toBe(true);
        expect(frame.payload.applied_actions).toEqual([
            "resume_last_active_file:act_9",
        ]);
        expect(frame.payload.errors).toEqual([]);
    });

    it("keeps the resume ack distinct from the UIPlan apply ack", () => {
        const socket = new FakeSocket();
        const client = connectedClient(socket);

        // A UIPlan apply ack and an execute_action ack for the SAME
        // intervention id must carry different phases so the daemon's
        // (intervention_id, phase) dedup key does not collapse them.
        client.sendInterventionApplied("iv_42", "apply", true, ["foldExcept"], []);
        client.sendInterventionApplied(
            "iv_42",
            "execute_action",
            true,
            ["resume_last_active_file:act_1"],
            [],
        );

        expect(socket.sent.length).toBe(2);
        const phases = socket.sent.map((f) => f.payload.phase);
        expect(phases).toEqual(["apply", "execute_action"]);
        // Distinct dedup keys: (iv_42, "apply") vs (iv_42, "execute_action").
        const keys = new Set(
            socket.sent.map(
                (f) => `${String(f.payload.intervention_id)}:${String(f.payload.phase)}`,
            ),
        );
        expect(keys.size).toBe(2);
    });

    it("reports a failed resume with success=false and the error string", () => {
        const socket = new FakeSocket();
        const client = connectedClient(socket);

        client.sendInterventionApplied(
            "iv_7",
            "execute_action",
            false,
            [],
            ["resume_last_active_file: empty target"],
        );

        const frame = socket.sent[0];
        expect(frame.payload.phase).toBe("execute_action");
        expect(frame.payload.success).toBe(false);
        expect(frame.payload.errors).toEqual([
            "resume_last_active_file: empty target",
        ]);
        expect(frame.payload.applied_actions).toEqual([]);
    });
});
