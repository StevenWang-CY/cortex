/** Pure presentation model for the popup's transport and support state. */

import type { PulseReadinessReason } from "../types/generated/cortex_schemas";

export interface Biometrics {
    heart_rate: number | null;
    hrv_rmssd: number | null;
    blink_rate: number | null;
    head_neck_flexion_score: number | null;
    head_neck_flexion_angle: number | null;
    head_neck_flexion_dwell_seconds: number | null;
    head_neck_proxy_available: boolean;
}

/**
 * The single extension-side declaration of the daemon's state estimate.
 * ``background.ts`` and the popup both import it; ``reasons`` is emitted on
 * every STATE_UPDATE but only the worker reads it.
 */
export interface CortexState {
    state: string;
    support_state?: string;
    status?: "estimated" | "insufficient_evidence" | "warming_up";
    confidence: number;
    evidence_coverage?: number;
    scores: Record<string, number>;
    signal_quality: Record<string, number>;
    dwell_seconds: number;
    reasons?: string[];
    biometrics?: Biometrics;
    capture?: {
        frames_flowing?: boolean;
        face_detected?: boolean;
        stale?: boolean;
        pulse_unavailable?: PulseReadinessReason | null;
    };
    store?: { degraded?: boolean };
}

export interface FocusSnapshot {
    elapsedMs: number;
    focusMs: number;
    focusPct: number;
    distractionsBlocked: number;
    longestStreakMin: number;
    currentStreakMs: number;
    goal: string;
    autoArmed?: boolean;
    preset?: string;
    endsAt?: number | null;
}

export interface DailyStats {
    date: string;
    totalFocusMin: number;
    totalSessionMin: number;
    sessions: number;
    distractionsBlocked: number;
    longestStreakMin: number;
}

export type ExecutionMode = "suggest_only" | "authorized" | "research_autonomous";

export function parseExecutionMode(value: unknown): ExecutionMode {
    return value === "authorized" || value === "research_autonomous"
        ? value
        : "suggest_only";
}

export interface MicroStep {
    text: string;
    status: "pending" | "done" | "skipped";
}

export function normaliseMicroSteps(raw: unknown): MicroStep[] {
    if (!Array.isArray(raw)) return [];
    const result: MicroStep[] = [];
    for (const entry of raw) {
        if (typeof entry === "string") {
            if (entry.length > 0) result.push({ text: entry, status: "pending" });
            continue;
        }
        if (!entry || typeof entry !== "object") continue;
        const candidate = entry as Record<string, unknown>;
        const text = typeof candidate.text === "string" ? candidate.text : "";
        const rawStatus = candidate.status;
        const status: MicroStep["status"] = rawStatus === "done" || rawStatus === "skipped"
            ? rawStatus
            : "pending";
        if (text.length > 0) result.push({ text, status });
    }
    return result;
}

/**
 * Compare only ``major.minor``. A DMG patch and an extension patch ship on
 * different cadences; the negotiated wire protocol (``PROTOCOL_ERROR``)
 * guards real incompatibility, so a patch-level skew must never block the
 * popup. Unknown versions are treated as compatible.
 */
export function versionsCompatible(
    daemonVersion: string | null | undefined,
    expectedVersion: string | null | undefined,
): boolean {
    const parse = (value: string | null | undefined): [number, number] | null => {
        if (typeof value !== "string") return null;
        const match = /^v?(\d+)\.(\d+)/.exec(value.trim());
        if (!match) return null;
        return [Number(match[1]), Number(match[2])];
    };
    const left = parse(daemonVersion);
    const right = parse(expectedVersion);
    if (!left || !right) return true;
    return left[0] === right[0] && left[1] === right[1];
}

export type ConnectivityState =
    | "ok"
    | "not_installed"
    | "installed_no_daemon"
    | "installed_version_mismatch"
    | "handshake_failed";

export function classifyConnectivity(input: {
    connected: boolean;
    nativeHostStatus: "present" | "missing" | "unknown";
    daemonVersion: string | null;
    expectedVersion: string;
    handshakeError: string | null;
}): ConnectivityState {
    if (input.connected && input.handshakeError) return "handshake_failed";
    if (input.connected) {
        return versionsCompatible(input.daemonVersion, input.expectedVersion)
            ? "ok"
            : "installed_version_mismatch";
    }
    return input.nativeHostStatus === "missing"
        ? "not_installed"
        : "installed_no_daemon";
}

/**
 * Whether the state blocks the connected UI. A version skew is shown as a
 * dismissible banner over the live popup; it never hides the session.
 */
export function connectivityBlocksSession(state: ConnectivityState): boolean {
    return state !== "ok" && state !== "installed_version_mismatch";
}

export type ConnectivityAction = "launch" | "install" | "handshake";

export interface ConnectivityViewModel {
    title: string;
    body: string;
    ctaLabel: string;
    action: ConnectivityAction;
    testId: string;
    disabled: boolean;
}

/** Copy shown while the launch request is in flight. */
export const LAUNCH_PENDING_STATUS = "Connecting…";
/** Copy shown when the launch request did not produce a connection. */
export const LAUNCH_FAILED_STATUS =
    "Cortex didn't start. Open Cortex from Applications, then try again.";
/** Copy shown while the worker is reconnecting after a drop. */
export const RECONNECTING_STATUS = "Reconnecting…";

export function connectivityViewModel(input: {
    state: ConnectivityState;
    launching: boolean;
    launchError: boolean;
    launchStatus: string;
    expectedVersion: string;
    daemonVersion: string | null;
    handshakeError: string | null;
    nativeHostError?: string | null;
    reconnecting?: boolean;
}): ConnectivityViewModel {
    if (input.launching) {
        return {
            title: "Starting Cortex",
            body: input.launchStatus || LAUNCH_PENDING_STATUS,
            ctaLabel: "Starting…",
            action: "launch",
            testId: "conn-state-launching",
            disabled: true,
        };
    }
    if (input.state === "not_installed") {
        return {
            title: "Cortex app isn't linked to this browser",
            body: "Open Cortex, choose Connect Extensions, and pick this browser. Then quit the browser fully (Cmd+Q) and reopen it.",
            ctaLabel: "Finish setup",
            action: "install",
            testId: "conn-state-not_installed",
            disabled: false,
        };
    }
    if (input.state === "installed_version_mismatch") {
        return {
            title: "Cortex needs an update",
            body: "This extension and the Cortex app are on different versions. Update Cortex so they stay in step.",
            ctaLabel: "Open Cortex",
            action: "launch",
            testId: "conn-state-installed_version_mismatch",
            disabled: false,
        };
    }
    if (input.state === "handshake_failed") {
        // Never print the raw code — it is a diagnostic, not guidance.
        return {
            title: "Cortex couldn't verify this browser",
            body: "Open Cortex, choose Connect Extensions, and finish setup for this browser. Then quit the browser fully (Cmd+Q) and reopen it.",
            ctaLabel: "Try again",
            action: "handshake",
            testId: "conn-state-handshake_failed",
            disabled: false,
        };
    }
    if (input.reconnecting) {
        return {
            title: "Reconnecting to Cortex",
            body: RECONNECTING_STATUS,
            ctaLabel: "Open Cortex",
            action: "launch",
            testId: "conn-state-installed_no_daemon",
            disabled: false,
        };
    }
    return {
        title: "Cortex isn't running",
        body: input.launchError
            ? LAUNCH_FAILED_STATUS
            : (input.launchStatus || "Open Cortex to start reading your signals."),
        ctaLabel: input.launchError ? "Try again" : "Open Cortex",
        action: "launch",
        testId: "conn-state-installed_no_daemon",
        disabled: false,
    };
}

export interface SupportStateViewModel {
    stateKey: string;
    label: string;
    captureStale: boolean;
    storeDegraded: boolean;
    bioStatusMessage: string;
}

export function supportStateViewModel(
    state: CortexState | null,
    connected: boolean,
    labels: Readonly<Record<string, string>> = {},
): SupportStateViewModel {
    const estimateReady = state?.status === undefined || state.status === "estimated";
    const stateKey = estimateReady ? (state?.state ?? "") : "UNKNOWN";
    const label = state
        ? state.status === "warming_up"
            ? "Still gathering"
            : state.status === "insufficient_evidence"
                ? "Not enough evidence"
                : labels[stateKey] || "Status unavailable"
        : connected
            ? "Connecting…"
            : "Not running";
    const framesFlowing = state?.capture?.frames_flowing ?? true;
    const faceDetected = state?.capture?.face_detected ?? true;
    return {
        stateKey,
        label,
        captureStale: state?.capture?.stale === true,
        storeDegraded: state?.store?.degraded === true,
        bioStatusMessage: !state
            ? "Connecting to Cortex…"
            : !framesFlowing
                ? "Camera offline — open System Settings → Privacy & Security → Camera"
                : !faceDetected
                    ? "Looking for your face…"
                    : pulseUnavailableCopy(state.capture?.pulse_unavailable),
    };
}

const PULSE_MISSING_REASON_COPY: Readonly<Record<string, string>> = {
    no_face: "Stay in view for a pulse reading",
    low_light: "Too dark for a pulse reading — add some light",
    saturated: "Too bright for a pulse reading — reduce glare",
    motion: "Hold still for a pulse reading",
    occluded: "Face partly covered — clear the camera's view",
    camera_warmup: "Camera warming up…",
    frame_dropped: "Camera frames are dropping — close other camera apps",
    permission: "Camera permission needed for a pulse reading",
    source_disconnected: "Camera disconnected",
};

/**
 * Consumer copy for the daemon's pulse-readiness reason (v0.4.0, audit S10).
 * Mirrors ``cortex.apps.desktop_shell.view_models.pulse_unavailable_copy`` so
 * the popup and the desktop dashboard say the same thing.
 */
export function pulseUnavailableCopy(reason: unknown): string {
    const fallback = "Reading your pulse…";
    if (!reason || typeof reason !== "object") return fallback;
    const r = reason as { code?: unknown; missing_reason?: unknown; observed?: unknown; required?: unknown };
    const code = typeof r.code === "string" ? r.code : "";
    const missing = typeof r.missing_reason === "string" ? r.missing_reason.toLowerCase() : "";
    if (code === "filling") {
        const observed = typeof r.observed === "number" ? r.observed : null;
        const required = typeof r.required === "number" ? r.required : null;
        if (observed !== null && required !== null && required > 0) {
            return `Reading your pulse… ${Math.round(Math.min(observed, required))} of ${Math.round(required)} s`;
        }
        return fallback;
    }
    if (code === "no_observations") return "Waiting for the camera…";
    if (code === "duplicate_timestamps" || code === "too_few_valid_samples") return fallback;
    if (code === "motion_fraction_above_cap") return PULSE_MISSING_REASON_COPY.motion;
    if (missing in PULSE_MISSING_REASON_COPY) return PULSE_MISSING_REASON_COPY[missing];
    if (code === "gap_too_long") return PULSE_MISSING_REASON_COPY.no_face;
    if (code === "valid_fraction_below_gate") return "Not enough usable frames yet — stay in view with steady light";
    return fallback;
}
