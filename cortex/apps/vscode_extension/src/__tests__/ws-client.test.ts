/**
 * CortexWSClient connection truthfulness (audit A1 / A4 / A5 / A7).
 *
 *  - Non-loopback daemon URLs are refused before a socket is opened.
 *  - Without a capability token the client warns once (naming the token
 *    path), sends nothing, never reports "connected" and backs off.
 *  - "Connected" is only reported after AUTH_OK; IDENTIFY follows AUTH_OK;
 *    the "Connected to daemon" toast fires once per host, not per reconnect.
 *  - Daemon close code 1011 is surfaced to the user.
 *  - PROTOCOL_ERROR shows one error with a Retry action that re-dials.
 *  - WHY_DETAIL requests reject with WhyDetailTimeoutError after 5 s.
 */

import * as vscode from "vscode";
import * as wsModule from "ws";
import {
    CortexWSClient,
    WhyDetailTimeoutError,
    isLoopbackDaemonUrl,
} from "../ws-client";

jest.mock("ws", () => {
    const { EventEmitter } = jest.requireActual("events") as typeof import("events");
    class FakeWebSocket extends EventEmitter {
        static instances: FakeWebSocket[] = [];
        url: string;
        sent: string[] = [];
        closed: Array<{ code?: number; reason?: string }> = [];
        terminated = 0;
        constructor(url: string) {
            super();
            this.url = url;
            FakeWebSocket.instances.push(this);
        }
        send(raw: string): void { this.sent.push(raw); }
        close(code?: number, reason?: string): void { this.closed.push({ code, reason }); }
        terminate(): void { this.terminated += 1; }
        ping(): void { /* frame-level ping is a no-op for the fake */ }
    }
    return { __esModule: true, default: FakeWebSocket, __fake: FakeWebSocket };
});

interface FakeSocketShape {
    url: string;
    sent: string[];
    closed: Array<{ code?: number; reason?: string }>;
    terminated: number;
    emit(event: string, ...args: unknown[]): boolean;
}

function sockets(): FakeSocketShape[] {
    return (wsModule as unknown as { __fake: { instances: FakeSocketShape[] } })
        .__fake.instances;
}

function latest(): FakeSocketShape {
    const all = sockets();
    return all[all.length - 1];
}

function frames(socket: FakeSocketShape): Array<{ type: string; payload: Record<string, unknown> }> {
    return socket.sent.map((raw) => JSON.parse(raw));
}

const TOKEN = "t".repeat(40);
const TOKEN_PATH = "/tmp/cortex-test/auth.token";

function makeClient(
    url = "ws://127.0.0.1:9473",
    token: string | null = TOKEN,
): CortexWSClient {
    return new CortexWSClient(url, "vscode_test_instance_0001", {
        readToken: () => ({ token, path: TOKEN_PATH }),
    });
}

let sequence = 0;
function daemonFrame(type: string, payload: Record<string, unknown>, extra: Record<string, unknown> = {}): string {
    sequence += 1;
    return JSON.stringify({
        type,
        payload,
        schema_version: "2.0",
        protocol_version: "2.0",
        event_id: `evt-${sequence}`,
        sequence,
        timestamp: 1,
        ...extra,
    });
}

async function flushPromises(): Promise<void> {
    for (let i = 0; i < 6; i++) {
        await Promise.resolve();
    }
}

const mockWindow = vscode.window as unknown as {
    showWarningMessage: jest.Mock;
    showErrorMessage: jest.Mock;
    setStatusBarMessage: jest.Mock;
};
const mockCommands = vscode.commands as unknown as { executeCommand: jest.Mock };

beforeEach(() => {
    sockets().length = 0;
    jest.useFakeTimers({ doNotFake: ["nextTick", "queueMicrotask", "setImmediate"] });
    mockWindow.showWarningMessage.mockReset();
    mockWindow.showWarningMessage.mockImplementation(() => Promise.resolve(undefined));
    mockWindow.showErrorMessage.mockReset();
    mockWindow.showErrorMessage.mockImplementation(() => Promise.resolve(undefined));
    mockWindow.setStatusBarMessage.mockReset();
    mockCommands.executeCommand.mockReset();
});

afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
});

describe("isLoopbackDaemonUrl (A1)", () => {
    it.each([
        "ws://127.0.0.1:9473",
        "ws://127.0.0.1",
        "ws://localhost:9473",
        "ws://LOCALHOST:9473",
        "ws://[::1]:9473",
        "wss://127.0.0.1:9473",
        "ws://127.1.2.3:9473",
    ])("accepts %s", (url) => {
        expect(isLoopbackDaemonUrl(url)).toBe(true);
    });

    it.each([
        "ws://10.0.0.5:9473",
        "ws://daemon.example.com:9473",
        "ws://127.0.0.1.evil.example:9473",
        "ws://localhost.evil.example:9473",
        "ws://user:pw@127.0.0.1:9473",
        "http://127.0.0.1:9472",
        "ws://0.0.0.0:9473",
        "not a url",
        "",
    ])("refuses %s", (url) => {
        expect(isLoopbackDaemonUrl(url)).toBe(false);
    });
});

describe("CortexWSClient.connect – non-loopback refusal (A1)", () => {
    it("never opens a socket to a non-loopback host and tells the user once", () => {
        const client = makeClient("ws://daemon.example.com:9473");
        const onConn = jest.fn();
        client.onConnectionChange(onConn);

        client.connect();
        client.connect();

        expect(sockets()).toHaveLength(0);
        expect(client.connected).toBe(false);
        expect(onConn).not.toHaveBeenCalled();
        expect(mockWindow.showErrorMessage).toHaveBeenCalledTimes(1);
        expect(String(mockWindow.showErrorMessage.mock.calls[0][0])).toContain(
            "daemon.example.com",
        );
        // No reconnect cycle is armed for a URL that cannot fix itself.
        jest.advanceTimersByTime(60_000);
        expect(sockets()).toHaveLength(0);
    });

    it("setUrl refuses a non-loopback host and keeps the previous URL", () => {
        const client = makeClient();
        client.setUrl("ws://10.0.0.5:9473");
        expect(client.url).toBe("ws://127.0.0.1:9473");
        expect(mockWindow.showErrorMessage).toHaveBeenCalledTimes(1);
    });
});

describe("CortexWSClient – missing capability token (A4)", () => {
    it("warns once naming the token path, sends nothing and never reports connected", async () => {
        const client = makeClient("ws://127.0.0.1:9473", null);
        const onConn = jest.fn();
        client.onConnectionChange(onConn);

        client.connect();
        expect(sockets()).toHaveLength(1);
        latest().emit("open");

        // No AUTH, no IDENTIFY — nothing at all reaches the daemon.
        expect(latest().sent).toHaveLength(0);
        expect(client.connected).toBe(false);
        expect(onConn).not.toHaveBeenCalled();
        expect(mockWindow.setStatusBarMessage).not.toHaveBeenCalled();
        expect(mockWindow.showWarningMessage).toHaveBeenCalledTimes(1);
        const [message, action] = mockWindow.showWarningMessage.mock.calls[0];
        expect(String(message)).toContain(TOKEN_PATH);
        expect(action).toBe("Open Cortex");
        // Our side closes so the backoff cycle re-reads the file later.
        expect(latest().closed[0]?.code).toBe(1000);

        // Backoff reconnect re-checks the token but does not warn again.
        latest().emit("close", 1000, Buffer.from(""));
        jest.advanceTimersByTime(3_000);
        expect(sockets()).toHaveLength(2);
        latest().emit("open");
        expect(latest().sent).toHaveLength(0);
        expect(mockWindow.showWarningMessage).toHaveBeenCalledTimes(1);
        expect(onConn).not.toHaveBeenCalled();
        await flushPromises();
    });

    it("the Open Cortex action reveals the Cortex panel", async () => {
        mockWindow.showWarningMessage.mockImplementation(() => Promise.resolve("Open Cortex"));
        const client = makeClient("ws://127.0.0.1:9473", null);
        client.connect();
        latest().emit("open");
        await flushPromises();
        expect(mockCommands.executeCommand).toHaveBeenCalledWith("cortex.showPanel");
    });
});

describe("CortexWSClient – AUTH_OK gates the connected state (A4)", () => {
    it("sends AUTH first, reports connected and sends IDENTIFY only after AUTH_OK", () => {
        const client = makeClient();
        const onConn = jest.fn();
        client.onConnectionChange(onConn);

        client.connect();
        latest().emit("open");

        let sent = frames(latest());
        expect(sent.map((f) => f.type)).toEqual(["AUTH"]);
        expect(sent[0].payload.auth_token).toBe(TOKEN);
        expect(client.connected).toBe(false);
        expect(onConn).not.toHaveBeenCalled();
        expect(mockWindow.setStatusBarMessage).not.toHaveBeenCalled();

        latest().emit(
            "message",
            Buffer.from(daemonFrame("AUTH_OK", { selected_protocol_version: "2.0" })),
        );

        sent = frames(latest());
        expect(sent.map((f) => f.type)).toEqual(["AUTH", "IDENTIFY"]);
        expect(sent[1].payload.client_type).toBe("vscode");
        expect(sent[1].payload.client_instance_id).toBe("vscode_test_instance_0001");
        expect(client.connected).toBe(true);
        expect(onConn).toHaveBeenCalledTimes(1);
        expect(onConn).toHaveBeenCalledWith(true);
        expect(mockWindow.setStatusBarMessage).toHaveBeenCalledTimes(1);
    });

    it("flushes the offline outbox after AUTH_OK, after IDENTIFY", () => {
        const client = makeClient();
        client.sendUserAction("dismissed", "iv-queued");
        client.connect();
        latest().emit("open");
        latest().emit(
            "message",
            Buffer.from(daemonFrame("AUTH_OK", { selected_protocol_version: "2.0" })),
        );
        expect(frames(latest()).map((f) => f.type)).toEqual([
            "AUTH",
            "IDENTIFY",
            "USER_ACTION",
        ]);
    });

    it("suppresses the Connected toast on reconnects but re-notifies handlers", () => {
        const client = makeClient();
        const onConn = jest.fn();
        client.onConnectionChange(onConn);

        client.connect();
        latest().emit("open");
        latest().emit(
            "message",
            Buffer.from(daemonFrame("AUTH_OK", { selected_protocol_version: "2.0" })),
        );
        expect(mockWindow.setStatusBarMessage).toHaveBeenCalledTimes(1);

        // Daemon restarts.
        latest().emit("close", 1006, Buffer.from(""));
        expect(client.connected).toBe(false);
        expect(onConn).toHaveBeenLastCalledWith(false);

        jest.advanceTimersByTime(3_000);
        expect(sockets()).toHaveLength(2);
        latest().emit("open");
        latest().emit(
            "message",
            Buffer.from(daemonFrame("AUTH_OK", { selected_protocol_version: "2.0" })),
        );
        expect(client.connected).toBe(true);
        expect(onConn).toHaveBeenLastCalledWith(true);
        expect(onConn).toHaveBeenCalledTimes(3);
        expect(mockWindow.setStatusBarMessage).toHaveBeenCalledTimes(1);
    });

    it("tears the socket down when AUTH_OK never arrives", () => {
        const client = makeClient();
        client.connect();
        latest().emit("open");
        expect(latest().terminated).toBe(0);
        jest.advanceTimersByTime(10_000);
        expect(latest().terminated).toBe(1);
        expect(client.connected).toBe(false);
    });
});

describe("CortexWSClient – daemon close 1011 is surfaced (A4)", () => {
    it("shows one warning per close reason", () => {
        const client = makeClient();
        client.connect();
        latest().emit("open");
        latest().emit("close", 1011, Buffer.from("invalid auth token"));

        expect(client.connected).toBe(false);
        expect(mockWindow.showWarningMessage).toHaveBeenCalledTimes(1);
        const message = String(mockWindow.showWarningMessage.mock.calls[0][0]);
        expect(message).toContain("1011");
        expect(message).toContain("invalid auth token");

        // Reconnect + identical rejection → no second toast.
        jest.advanceTimersByTime(3_000);
        latest().emit("open");
        latest().emit("close", 1011, Buffer.from("invalid auth token"));
        expect(mockWindow.showWarningMessage).toHaveBeenCalledTimes(1);

        // A different reason is a new episode.
        jest.advanceTimersByTime(6_000);
        latest().emit("open");
        latest().emit("close", 1011, Buffer.from("auth required"));
        expect(mockWindow.showWarningMessage).toHaveBeenCalledTimes(2);
    });

    it("an ordinary close is silent", () => {
        const client = makeClient();
        client.connect();
        latest().emit("open");
        latest().emit("close", 1006, Buffer.from(""));
        expect(mockWindow.showWarningMessage).not.toHaveBeenCalled();
    });
});

describe("CortexWSClient – PROTOCOL_ERROR (A5)", () => {
    it("shows the error once with Retry and re-dials when Retry is chosen", async () => {
        mockWindow.showErrorMessage.mockImplementation(() => Promise.resolve("Retry"));
        const client = makeClient();
        client.connect();
        latest().emit("open");
        const first = latest();

        first.emit(
            "message",
            Buffer.from(daemonFrame("PROTOCOL_ERROR", {
                code: "unsupported_protocol",
                offered_protocol_versions: ["2.0", "1.0"],
            })),
        );
        // A duplicate frame while the prompt is open must not stack.
        first.emit(
            "message",
            Buffer.from(daemonFrame("PROTOCOL_ERROR", { code: "unsupported_protocol" })),
        );

        expect(first.closed.some((c) => c.code === 1002)).toBe(true);
        expect(mockWindow.showErrorMessage).toHaveBeenCalledTimes(1);
        const [message, action] = mockWindow.showErrorMessage.mock.calls[0];
        expect(String(message)).toContain("unsupported_protocol");
        expect(action).toBe("Retry");

        await flushPromises();
        // Retry dialled a fresh socket.
        expect(sockets()).toHaveLength(2);
        expect(latest()).not.toBe(first);
    });

    it("does not schedule a silent reconnect after PROTOCOL_ERROR when the user declines", async () => {
        const client = makeClient();
        client.connect();
        latest().emit("open");
        latest().emit(
            "message",
            Buffer.from(daemonFrame("PROTOCOL_ERROR", { code: "unsupported_protocol" })),
        );
        latest().emit("close", 1002, Buffer.from("unsupported protocol"));
        await flushPromises();
        jest.advanceTimersByTime(60_000);
        expect(sockets()).toHaveLength(1);
        expect(mockWindow.showErrorMessage).toHaveBeenCalledTimes(1);
    });
});

describe("CortexWSClient – WHY_DETAIL correlation (A7)", () => {
    function connectedClient(): CortexWSClient {
        const client = makeClient();
        client.connect();
        latest().emit("open");
        latest().emit(
            "message",
            Buffer.from(daemonFrame("AUTH_OK", { selected_protocol_version: "2.0" })),
        );
        return client;
    }

    it("rejects with WhyDetailTimeoutError after 5 s", async () => {
        const client = connectedClient();
        const pending = client.sendWhyDetailRequest("iv-1");
        const settled = pending.then(
            () => "resolved",
            (err: unknown) => err,
        );
        jest.advanceTimersByTime(5_000);
        const outcome = await settled;
        expect(outcome).toBeInstanceOf(WhyDetailTimeoutError);
        expect((outcome as WhyDetailTimeoutError).timeoutMs).toBe(5000);
    });

    it("resolves with the daemon payload when the correlation id matches", async () => {
        const client = connectedClient();
        const pending = client.sendWhyDetailRequest("iv-2");
        const request = JSON.parse(latest().sent[latest().sent.length - 1]) as {
            type: string;
            correlation_id: string;
        };
        expect(request.type).toBe("WHY_DETAIL_REQUEST");
        latest().emit(
            "message",
            Buffer.from(daemonFrame(
                "WHY_DETAIL",
                { intervention_id: "iv-2", causal_signals: [{ name: "blink" }] },
                { correlation_id: request.correlation_id },
            )),
        );
        await expect(pending).resolves.toMatchObject({ intervention_id: "iv-2" });
    });
});
