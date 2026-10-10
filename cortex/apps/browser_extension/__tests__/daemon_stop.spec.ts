import { describe, expect, it, vi } from "vitest";
import { GRACEFUL_STOP_MS, stopDaemon, type StopPorts } from "../lib/daemon-stop";

function fixture(): { ports: StopPorts; now: () => number } {
    let time = 0;
    return {
        now: () => time,
        ports: {
            graceful: vi.fn(async () => undefined),
            health: vi.fn(async () => true),
            nativeStop: vi.fn(async () => ({ status: "stopped" })),
            launcherStop: vi.fn(async () => ({ status: "stopped" })),
            now: () => time,
            sleep: async (milliseconds) => { time += milliseconds; },
        },
    };
}

describe("verified daemon stop", () => {
    it("gives a responding daemon the complete graceful window before native escalation", async () => {
        const { ports, now } = fixture();
        ports.nativeStop = vi.fn(async () => {
            expect(now()).toBeGreaterThanOrEqual(20_000);
            return { status: "stopped" };
        });
        expect(await stopDaemon(ports)).toEqual({ ok: true });
        expect(now()).toBe(GRACEFUL_STOP_MS);
        expect(ports.launcherStop).not.toHaveBeenCalled();
    });

    it("endpoint loss still requires native process-exit verification", async () => {
        const { ports } = fixture();
        ports.health = vi.fn(async () => false);
        ports.nativeStop = vi.fn(async () => ({ status: "error", error: "Process remains alive" }));
        expect(await stopDaemon(ports)).toEqual({ ok: false, error: "Process remains alive" });
        expect(ports.launcherStop).not.toHaveBeenCalled();
    });

    it("can use the authenticated launcher when native messaging is unavailable", async () => {
        const { ports } = fixture();
        ports.graceful = vi.fn(async () => { throw new Error("401"); });
        ports.health = vi.fn(async () => false);
        ports.nativeStop = vi.fn(async () => { throw new Error("host missing"); });
        ports.launcherStop = vi.fn(async () => ({ status: "not_running" }));
        expect(await stopDaemon(ports)).toEqual({ ok: true });
        expect(ports.launcherStop).toHaveBeenCalledOnce();
    });

    it("does not overlap another stop chain when a native request times out", async () => {
        const { ports } = fixture();
        ports.health = vi.fn(async () => false);
        ports.nativeStop = vi.fn(async () => { throw new Error("native_host_timeout"); });
        expect((await stopDaemon(ports)).ok).toBe(false);
        expect(ports.launcherStop).not.toHaveBeenCalled();
    });

    it("fails honestly when no transport can verify shutdown", async () => {
        const { ports } = fixture();
        ports.health = vi.fn(async () => { throw new Error("timeout"); });
        ports.nativeStop = vi.fn(async () => { throw new Error("host missing"); });
        ports.launcherStop = vi.fn(async () => { throw new Error("401"); });
        expect((await stopDaemon(ports)).ok).toBe(false);
    });
});
