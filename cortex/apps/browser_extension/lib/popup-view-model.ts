/** Pure presentation model for the popup's transport and support state. */

export interface Biometrics {
    heart_rate: number | null;
    hrv_rmssd: number | null;
    blink_rate: number | null;
    head_neck_flexion_score: number | null;
    head_neck_flexion_angle: number | null;
    head_neck_flexion_dwell_seconds: number | null;
    head_neck_proxy_available: boolean;
}

export interface CortexState {
    state: string;
    support_state?: string;
    status?: "estimated" | "insufficient_evidence" | "warming_up";
    confidence: number;
    evidence_coverage?: number;
    scores: Record<string, number>;
    signal_quality: Record<string, number>;
    dwell_seconds: number;
    biometrics?: Biometrics;
    capture?: {
        frames_flowing?: boolean;
        face_detected?: boolean;
        stale?: boolean;
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
        return input.daemonVersion
            && input.expectedVersion
            && input.daemonVersion !== input.expectedVersion
            ? "installed_version_mismatch"
            : "ok";
    }
    return input.nativeHostStatus === "missing"
        ? "not_installed"
        : "installed_no_daemon";
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

export function connectivityViewModel(input: {
    state: ConnectivityState;
    launching: boolean;
    launchError: boolean;
    launchStatus: string;
    expectedVersion: string;
    daemonVersion: string | null;
    handshakeError: string | null;
    nativeHostError?: string | null;
}): ConnectivityViewModel {
    if (input.launching) {
        return {
            title: "Starting Cortex",
            body: input.launchStatus || "Launching daemon…",
            ctaLabel: "Starting…",
            action: "launch",
            testId: "conn-state-launching",
            disabled: true,
        };
    }
    if (input.state === "not_installed") {
        const diagnostic = input.nativeHostError
            ? ` Browser check: ${input.nativeHostError}.`
            : "";
        return {
            title: "Browser bridge unavailable",
            body: "Open Cortex → Connect Extensions, choose this browser, and finish the setup steps. Fully quit the browser with Cmd+Q before reopening it." + diagnostic,
            ctaLabel: "Open connection guide",
            action: "install",
            testId: "conn-state-not_installed",
            disabled: false,
        };
    }
    if (input.state === "installed_version_mismatch") {
        return {
            title: "Daemon version mismatch",
            body: `Extension expects v${input.expectedVersion}; daemon is v${input.daemonVersion ?? "?"}. Update the daemon or downgrade the extension to match.`,
            ctaLabel: "Restart daemon",
            action: "launch",
            testId: "conn-state-installed_version_mismatch",
            disabled: false,
        };
    }
    if (input.state === "handshake_failed") {
        return {
            title: "Handshake failed",
            body: input.handshakeError
                || "The daemon answered but rejected this extension's handshake. Check the local auth token.",
            ctaLabel: "Retry handshake",
            action: "handshake",
            testId: "conn-state-handshake_failed",
            disabled: false,
        };
    }
    return {
        title: "Not connected",
        body: input.launchStatus || "Launch daemon with camera",
        ctaLabel: input.launchError ? "Retry" : "Start Cortex",
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
            : "Disconnected";
    const framesFlowing = state?.capture?.frames_flowing ?? true;
    const faceDetected = state?.capture?.face_detected ?? true;
    return {
        stateKey,
        label,
        captureStale: state?.capture?.stale === true,
        storeDegraded: state?.store?.degraded === true,
        bioStatusMessage: !state
            ? "Connecting to daemon…"
            : !framesFlowing
                ? "Camera offline — open System Settings → Privacy & Security → Camera"
                : !faceDetected
                    ? "Looking for your face…"
                    : "Reading your pulse…",
    };
}
