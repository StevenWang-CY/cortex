/** One bounded shutdown chain; endpoint loss alone never proves process exit. */

export const GRACEFUL_STOP_MS = 20_000;
export const NATIVE_STOP_TIMEOUT_MS = 40_000;
// Includes an already-issued launch helper (up to 30s), auth/flush/grace,
// native verification, and a non-overlapping launcher fallback.
export const POPUP_STOP_TIMEOUT_MS = 150_000;

export type StopStatus = "idle" | "stopping" | "stopped" | "error";
export type StopResult = { ok: true } | { ok: false; error: string };

export interface StopPorts {
    graceful: () => Promise<void>;
    /** false only for an unavailable endpoint; timeouts remain alive/unknown. */
    health: () => Promise<boolean>;
    nativeStop: () => Promise<{ status: string; error?: string | null }>;
    launcherStop: () => Promise<{ status: string; error?: string | null }>;
    now: () => number;
    sleep: (milliseconds: number) => Promise<void>;
}

const success = (status: string): boolean => status === "stopped" || status === "not_running";

export async function stopDaemon(ports: StopPorts): Promise<StopResult> {
    try { await ports.graceful(); } catch { /* native helper can read its own token */ }
    const deadline = ports.now() + GRACEFUL_STOP_MS;
    while (ports.now() < deadline) {
        let alive = true;
        try { alive = await ports.health(); } catch { /* unknown is not stopped */ }
        if (!alive) break;
        await ports.sleep(Math.min(500, Math.max(0, deadline - ports.now())));
    }
    try {
        const result = await ports.nativeStop();
        if (success(result.status)) return { ok: true };
        // A completed authoritative failure must remain visible. Repeating the
        // same OS signals through another transport cannot make it safer.
        return { ok: false, error: result.error || "Cortex could not confirm shutdown. Try again from the app." };
    } catch (error) {
        if (error instanceof Error && error.message === "native_host_timeout") {
            // The helper may still be finishing. Do not launch a concurrent
            // destructive fallback while its outcome is unknown.
            return { ok: false, error: "Shutdown is taking longer than expected. Check Cortex before trying again." };
        }
    }
    try {
        const result = await ports.launcherStop();
        if (success(result.status)) return { ok: true };
        return { ok: false, error: result.error || "The launcher could not confirm shutdown." };
    } catch {
        return { ok: false, error: "Could not confirm shutdown. Open Cortex and stop the session there, or retry." };
    }
}
