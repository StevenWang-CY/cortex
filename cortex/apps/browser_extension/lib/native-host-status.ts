/**
 * Cached native-host reachability probe.
 *
 * Every ``chrome.runtime.sendNativeMessage`` spawns the native host process.
 * Probing on every socket close (including failed connects) plus the 24 s
 * keepalive reconnect used to spawn it continuously while the Cortex app was
 * closed. The cache answers from memory for ``ttlMs`` unless a caller
 * explicitly forces a fresh probe (popup open, install), and shares one
 * in-flight probe between concurrent callers.
 */

import { sendNativeHostMessage } from "./native-messaging";

export type NativeHostReachability = "present" | "missing";

export interface NativeHostProbeResult {
    status: NativeHostReachability;
    error: string | null;
    at: number;
}

export const NATIVE_HOST_PROBE_TTL_MS = 5 * 60 * 1_000;

export class NativeHostStatusCache {
    private cached: NativeHostProbeResult | null = null;
    private inFlight: Promise<NativeHostProbeResult> | null = null;

    constructor(
        private readonly ttlMs: number = NATIVE_HOST_PROBE_TTL_MS,
        private readonly send: typeof sendNativeHostMessage = sendNativeHostMessage,
    ) {}

    /** Last known result without touching the native host. */
    peek(): NativeHostProbeResult | null {
        return this.cached;
    }

    /**
     * Resolve reachability. ``force`` bypasses the cache; otherwise a result
     * younger than ``ttlMs`` is returned without spawning the host.
     */
    async probe(force = false, now = Date.now()): Promise<NativeHostProbeResult> {
        if (!force && this.cached && now - this.cached.at < this.ttlMs) {
            return this.cached;
        }
        if (this.inFlight) return this.inFlight;
        this.inFlight = (async () => {
            let result: NativeHostProbeResult;
            try {
                const response = await this.send(
                    { command: "status" },
                    { timeoutMs: 5_000 },
                );
                if (response.command !== "status") {
                    throw new Error("unexpected_native_host_response");
                }
                result = { status: "present", error: null, at: Date.now() };
            } catch (error) {
                result = {
                    status: "missing",
                    error: error instanceof Error ? error.message : String(error),
                    at: Date.now(),
                };
            }
            this.cached = result;
            return result;
        })();
        try {
            return await this.inFlight;
        } finally {
            this.inFlight = null;
        }
    }

    /** Test-only: forget the cached result. */
    reset(): void {
        this.cached = null;
        this.inFlight = null;
    }
}
