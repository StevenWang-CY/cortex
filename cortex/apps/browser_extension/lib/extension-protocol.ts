/**
 * Runtime-message protocol between the background worker and the popup /
 * new-tab surfaces (``chrome.runtime`` channel; never the WebSocket).
 *
 * These are discriminated unions with guards for the messages the surfaces
 * dispatch on. Anything not modelled here is still delivered as a
 * ``Record<string, unknown>``; the guards narrow only what a boundary
 * actually reads, so a renamed field fails the type-check instead of
 * silently rendering nothing.
 */

import { isApplyOutcome, type ApplyOutcome } from "./apply-state";

export interface ActionResultShape {
    action_id: string;
    success: boolean;
    message: string;
    reversible: boolean;
}

export interface ConnectivityDiagnosticPayload {
    native_host_status: "present" | "missing" | "unknown";
    native_host_error: string | null;
    daemon_version: string | null;
    handshake_error: string | null;
}

// --- background → popup / new tab ---------------------------------------

export interface ConnectionChangedMessage {
    type: "CONNECTION_CHANGED";
    connected: boolean;
}

export interface ConnectivityDiagnosticMessage {
    type: "CONNECTIVITY_DIAGNOSTIC";
    payload: ConnectivityDiagnosticPayload;
}

export interface InterventionAppliedMessage {
    type: "INTERVENTION_APPLIED";
    intervention_id: string;
    results: ActionResultShape[];
    outcome: ApplyOutcome;
}

export interface InterventionRestoreMessage {
    type: "INTERVENTION_RESTORE";
    payload: Record<string, unknown>;
}

export interface OverlayDismissedMessage {
    type: "OVERLAY_DISMISSED";
    intervention_id: string | null;
}

export interface StopIntentMessage {
    type: "STOP_INTENT";
    stopRequested: boolean;
}

export type PopupInboundMessage =
    | ConnectionChangedMessage
    | ConnectivityDiagnosticMessage
    | InterventionAppliedMessage
    | InterventionRestoreMessage
    | OverlayDismissedMessage
    | StopIntentMessage;

// --- popup / new tab / page panel → background ---------------------------

export type TerminalUserAction = "dismissed" | "expired" | "engaged" | "restore";

export interface ExecuteAllRecommendedRequest {
    type: "EXECUTE_ALL_RECOMMENDED";
    intervention_id: string;
    correlation_id?: string;
}

export interface UndoAllRecentRequest {
    type: "UNDO_ALL_RECENT";
    intervention_id: string;
    correlation_id?: string;
}

export interface UserActionRequest {
    type: "USER_ACTION";
    action: TerminalUserAction;
    intervention_id?: string | null;
    correlation_id?: string;
}

export interface StopCortexRequest {
    type: "STOP_CORTEX";
    correlation_id?: string;
}

export interface ConnectRequest {
    type: "CONNECT";
    correlation_id?: string;
}

export interface DistractionBlockedRequest {
    type: "DISTRACTION_BLOCKED";
    leave: "back" | "close";
}

export type BackgroundInboundMessage =
    | ExecuteAllRecommendedRequest
    | UndoAllRecentRequest
    | UserActionRequest
    | StopCortexRequest
    | ConnectRequest
    | DistractionBlockedRequest;

/** Response to ``EXECUTE_ALL_RECOMMENDED`` — the same outcome both surfaces render. */
export interface ExecuteAllRecommendedResponse {
    ok: boolean;
    results: ActionResultShape[];
    outcome: ApplyOutcome;
}

// --- guards ----------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isActionResultShape(value: unknown): value is ActionResultShape {
    return isRecord(value)
        && typeof value.action_id === "string"
        && typeof value.success === "boolean"
        && typeof value.message === "string"
        && typeof value.reversible === "boolean";
}

export function isConnectivityDiagnosticPayload(
    value: unknown,
): value is ConnectivityDiagnosticPayload {
    if (!isRecord(value)) return false;
    const status = value.native_host_status;
    return (status === "present" || status === "missing" || status === "unknown")
        && (value.native_host_error === null || typeof value.native_host_error === "string")
        && (value.daemon_version === null || typeof value.daemon_version === "string")
        && (value.handshake_error === null || typeof value.handshake_error === "string");
}

export function isInterventionAppliedMessage(
    value: unknown,
): value is InterventionAppliedMessage {
    return isRecord(value)
        && value.type === "INTERVENTION_APPLIED"
        && typeof value.intervention_id === "string"
        && Array.isArray(value.results)
        && value.results.every(isActionResultShape)
        && isApplyOutcome(value.outcome);
}

export function isExecuteAllRecommendedResponse(
    value: unknown,
): value is ExecuteAllRecommendedResponse {
    return isRecord(value)
        && typeof value.ok === "boolean"
        && Array.isArray(value.results)
        && value.results.every(isActionResultShape)
        && isApplyOutcome(value.outcome);
}

export function isPopupInboundMessage(value: unknown): value is PopupInboundMessage {
    if (!isRecord(value) || typeof value.type !== "string") return false;
    switch (value.type) {
        case "CONNECTION_CHANGED":
            return typeof value.connected === "boolean";
        case "CONNECTIVITY_DIAGNOSTIC":
            return isConnectivityDiagnosticPayload(value.payload);
        case "INTERVENTION_APPLIED":
            return isInterventionAppliedMessage(value);
        case "INTERVENTION_RESTORE":
            return isRecord(value.payload);
        case "OVERLAY_DISMISSED":
            return value.intervention_id === null
                || typeof value.intervention_id === "string";
        case "STOP_INTENT":
            return typeof value.stopRequested === "boolean";
        default:
            return false;
    }
}

const TERMINAL_ACTIONS = new Set<string>(["dismissed", "expired", "engaged", "restore"]);

export function isTerminalUserAction(value: unknown): value is TerminalUserAction {
    return typeof value === "string" && TERMINAL_ACTIONS.has(value);
}

export function isDistractionBlockedRequest(
    value: unknown,
): value is DistractionBlockedRequest {
    return isRecord(value)
        && value.type === "DISTRACTION_BLOCKED"
        && (value.leave === "back" || value.leave === "close");
}
