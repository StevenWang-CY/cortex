import { expect, it, vi } from "vitest";
import { getAllSockets, getLatestSocket } from "../test/mocks/websocket";

type Listener = (message: Record<string, unknown>, sender: unknown, respond: (response: unknown) => void) => unknown;

it.each(["http", "native"])("drains a pending %s launch before Stop without relaunching or enabling the camera", async (transport) => {
    vi.resetModules();
    const fake = globalThis.__cortexChrome;
    let finishLaunch: (() => void) | undefined;
    let launchCalls = 0;
    globalThis.fetch = vi.fn(async (input) => {
        const url = String(input);
        if (url.endsWith("/health")) throw new TypeError("connection refused");
        if (url.endsWith("/launch")) {
            if (transport === "native") throw new TypeError("launcher unavailable");
            launchCalls++;
            return new Promise<Response>(resolve => {
                finishLaunch = () => resolve({ ok: true, json: async () => ({ status: "starting" }) } as Response);
            });
        }
        return { ok: true, json: async () => ({}) } as Response;
    });
    fake.runtime.sendNativeMessage.mockImplementation((_host, request, callback) => {
        if (request.command === "get_auth_token") callback({ command: "get_auth_token", status: "ok", auth_token: "a".repeat(64) });
        else if (request.command === "launch") {
            launchCalls++;
            finishLaunch = () => callback({ command: "launch", status: "launched" });
        } else if (request.command === "stop") callback({ command: "stop", status: "stopped" });
        else callback({ command: "status", status: "stopped" });
    });
    await import("../background");
    await new Promise(resolve => setTimeout(resolve, 0));
    getLatestSocket()!.__open();
    const listener = fake.runtime.onMessage.addListener.mock.calls[0][0] as Listener;
    const start = () => new Promise(resolve => listener({ type: "LAUNCH_CORTEX" }, {}, resolve));
    const firstLaunch = start();
    const duplicateLaunch = start();
    await vi.waitFor(() => expect(finishLaunch).toBeTypeOf("function"));

    let stopResponse: unknown;
    const stopped = new Promise(resolve => listener({ type: "STOP_CORTEX" }, {}, response => {
        stopResponse = response;
        resolve(response);
    }));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(stopResponse).toBeUndefined();
    expect(fake.runtime.sendNativeMessage.mock.calls.some(call => call[1].command === "stop")).toBe(false);
    expect(vi.mocked(fetch).mock.calls.some(call => String(call[0]).endsWith("/shutdown"))).toBe(false);

    finishLaunch!();
    expect(await firstLaunch).toMatchObject({ ok: false, status: "cancelled" });
    expect(await duplicateLaunch).toMatchObject({ ok: false, status: "cancelled" });
    expect(await stopped).toEqual({ ok: true });
    expect(launchCalls).toBe(1);
    const cameraEnables = getAllSockets().flatMap(socket => socket.sent)
        .map(frame => JSON.parse(frame))
        .filter(frame => frame.type === "SETTINGS_SYNC" && frame.payload?.webcam_enabled === true);
    expect(cameraEnables).toEqual([]);
    expect(fake.storage.session.__peek().cortex_stop_requested).toBe(true);
});
