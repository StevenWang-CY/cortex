/**
 * Generic-phrase filters shared by the popup card and the injected page
 * panel model. LLM plans sometimes pad tab reasons, error analyses, and
 * micro-steps with placeholder text; surfaces hide those rather than
 * printing "not relevant to your task" under every tab.
 */

export const GENERIC_TAB_REASON_PHRASES: readonly string[] = [
    "not essential for",
    "not relevant to",
    "not related to",
    "may be distracting",
    "could be a distraction",
    "is a distraction",
    "not needed for",
    "distracting you from",
    "not useful for",
];

export const GENERIC_ERROR_PHRASES: readonly string[] = [
    "no specific errors",
    "no errors detected",
    "not applicable",
    "no error",
    "n/a",
    "none detected",
];

export const GENERIC_STEP_PHRASES: readonly string[] = [
    "take a moment to breathe",
    "take a break",
    "focus on your current task",
    "continue focusing",
    "focus on the task at hand",
    "stay focused",
    "keep going",
    "take a deep breath",
];

function containsAny(text: string, phrases: readonly string[]): boolean {
    const lowered = text.toLowerCase();
    return phrases.some((phrase) => lowered.includes(phrase));
}

/** Return the tab reason, or "" when it is a generic placeholder. */
export function cleanTabReason(raw: unknown): string {
    const text = typeof raw === "string" ? raw.trim() : "";
    return text && !containsAny(text, GENERIC_TAB_REASON_PHRASES) ? text : "";
}

/** True when the error analysis carries a real root cause. */
export function hasRealErrorAnalysis(rootCause: unknown): rootCause is string {
    return typeof rootCause === "string"
        && rootCause.trim().length > 0
        && !containsAny(rootCause, GENERIC_ERROR_PHRASES);
}

/** True when a micro-step says something more than "take a breath". */
export function isSpecificStep(text: string): boolean {
    return text.trim().length > 0 && !containsAny(text, GENERIC_STEP_PHRASES);
}
