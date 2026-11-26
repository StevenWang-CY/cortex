/** Shared rules for showing live evidence on browser surfaces. */
import { supportStateViewModel, type CortexState } from "./popup-view-model";

// UI continuity bound: 20 missed updates at the daemon's 500 ms cadence.
// This is not a claim about the validity of a physiological measurement.
export const STATE_FRESHNESS_MS = 10_000;

function record(value: unknown): Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value)
        ? value as Record<string, unknown> : {};
}

function numberOrNull(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Preserve optional fields without letting malformed numbers reach the UI. */
export function statePresentationFields(raw: unknown): Pick<CortexState, "biometrics" | "capture" | "store"> {
    const state = record(raw);
    const bio = record(state.biometrics);
    const capture = record(state.capture);
    const reason = record(capture.pulse_unavailable);
    const result: Pick<CortexState, "biometrics" | "capture" | "store"> = {};
    if (state.biometrics && typeof state.biometrics === "object") {
        result.biometrics = {
            heart_rate: numberOrNull(bio.heart_rate),
            hrv_rmssd: numberOrNull(bio.hrv_rmssd),
            hrv_sdnn: numberOrNull(bio.hrv_sdnn),
            respiration_rate: numberOrNull(bio.respiration_rate),
            strain_load_ratio: numberOrNull(bio.strain_load_ratio),
            blink_rate: numberOrNull(bio.blink_rate),
            head_neck_flexion_score: numberOrNull(bio.head_neck_flexion_score),
            head_neck_flexion_angle: numberOrNull(bio.head_neck_flexion_angle),
            head_neck_flexion_dwell_seconds: numberOrNull(bio.head_neck_flexion_dwell_seconds),
            head_neck_proxy_available: bio.head_neck_proxy_available === true,
        };
    }
    if (state.capture && typeof state.capture === "object") {
        result.capture = {
            ...(typeof capture.frames_flowing === "boolean" ? { frames_flowing: capture.frames_flowing } : {}),
            ...(typeof capture.face_detected === "boolean" ? { face_detected: capture.face_detected } : {}),
            stale: capture.stale === true,
            pulse_unavailable: typeof reason.code === "string" ? {
                code: reason.code,
                message: typeof reason.message === "string" ? reason.message : "",
                observed: numberOrNull(reason.observed),
                required: numberOrNull(reason.required),
                missing_reason: typeof reason.missing_reason === "string" ? reason.missing_reason : null,
            } : null,
        };
    }
    if (state.store && typeof state.store === "object") {
        result.store = { degraded: record(state.store).degraded === true };
    }
    return result;
}

/** Age travels as a duration; monotonic clocks from different contexts never mix. */
export function stateAgeMs(ageAtReceipt: number | null, receivedAt: number, now = performance.now()): number {
    if (ageAtReceipt === null || !Number.isFinite(ageAtReceipt) || ageAtReceipt < 0
        || !Number.isFinite(receivedAt) || !Number.isFinite(now) || now < receivedAt) {
        return Infinity;
    }
    return ageAtReceipt + now - receivedAt;
}

export function currentSignalView(
    state: CortexState | null,
    connected: boolean,
    ageMs: number,
    labels: Readonly<Record<string, string>>,
) {
    const stale = connected && state !== null
        && (!Number.isFinite(ageMs) || ageMs < 0 || ageMs >= STATE_FRESHNESS_MS);
    const current = connected && !stale ? state : null;
    const view = supportStateViewModel(current, connected, labels);
    const pulse = numberOrNull(current?.biometrics?.heart_rate);
    const captureReady = current?.capture?.stale !== true
        && current?.capture?.frames_flowing !== false
        && current?.capture?.face_detected !== false;
    const heartRate = captureReady && !current?.capture?.pulse_unavailable
        && pulse !== null && pulse >= 30 && pulse <= 220 ? Math.round(pulse) : null;
    const blink = numberOrNull(current?.biometrics?.blink_rate);
    // The slower camera measurements share the capture lifetime of the pulse:
    // a paused camera or an absent face clears them with it. The bounds are
    // the daemon's own plausibility ranges, repeated so a malformed frame
    // cannot draw an impossible number.
    const sdnn = numberOrNull(current?.biometrics?.hrv_sdnn);
    const breath = numberOrNull(current?.biometrics?.respiration_rate);
    const load = numberOrNull(current?.biometrics?.strain_load_ratio);
    return {
        ...view,
        stale,
        stateKey: connected && !stale ? view.stateKey : "UNKNOWN",
        label: stale ? "Waiting for fresh signals" : view.label,
        heartRate,
        blinkRate: captureReady && blink !== null && blink >= 0 ? Math.round(blink) : null,
        hrvSdnn: captureReady && sdnn !== null && sdnn >= 1 && sdnn <= 300 ? Math.round(sdnn) : null,
        breathRate: captureReady && breath !== null && breath >= 3 && breath <= 45
            ? Math.round(breath) : null,
        bodyLoadPercent: captureReady && load !== null && load >= 0
            ? Math.min(999, Math.round(load * 100)) : null,
        bioStatusMessage: !connected ? "Cortex is resting"
            : stale ? "Waiting for fresh signals…"
            : view.captureStale ? "Camera signal paused — waiting for fresh frames"
            : view.bioStatusMessage,
    };
}
