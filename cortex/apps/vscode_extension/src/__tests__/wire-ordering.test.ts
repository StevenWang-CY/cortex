import { CortexWSClient } from "../ws-client";

type MessageDriver = {
    _handleMessage(raw: string): void;
};

function deliver(
    client: CortexWSClient,
    type: "STATE_UPDATE" | "INTERVENTION_TRIGGER",
    sequence: number,
    eventId: string,
): void {
    (client as unknown as MessageDriver)._handleMessage(JSON.stringify({
        type,
        payload: { sequence },
        schema_version: "2.0",
        protocol_version: "2.0",
        event_id: eventId,
        sent_at_unix_ms: 1,
        sent_at_mono_ns: sequence,
        boot_id: "00000000-0000-0000-0000-000000000001",
        timestamp: 0.001,
        sequence,
    }));
}

describe("CortexWSClient v2 ordering and idempotency", () => {
    it("drops reordered and duplicate STATE_UPDATE frames", () => {
        const client = new CortexWSClient("ws://127.0.0.1:9473");
        const applied: number[] = [];
        client.onStateUpdate((payload) => applied.push(payload.sequence as number));

        deliver(client, "STATE_UPDATE", 10, "event-10");
        deliver(client, "STATE_UPDATE", 9, "event-9");
        deliver(client, "STATE_UPDATE", 11, "event-10");
        deliver(client, "STATE_UPDATE", 12, "event-12");

        expect(applied).toEqual([10, 12]);
    });

    it("scopes sequence ordering by message type", () => {
        const client = new CortexWSClient("ws://127.0.0.1:9473");
        const states: number[] = [];
        const interventions: number[] = [];
        client.onStateUpdate((payload) => states.push(payload.sequence as number));
        client.onInterventionTrigger((payload) => {
            interventions.push(payload.sequence as number);
        });

        deliver(client, "STATE_UPDATE", 100, "state-100");
        deliver(client, "INTERVENTION_TRIGGER", 1, "intervention-1");

        expect(states).toEqual([100]);
        expect(interventions).toEqual([1]);
    });
});
