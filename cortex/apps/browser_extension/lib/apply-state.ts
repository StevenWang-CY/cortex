/**
 * One apply state machine shared by the popup card and the injected page
 * panel.
 *
 * The background worker reduces the executor's per-action results into an
 * ``ApplyOutcome`` once, then hands the same object to both surfaces (as the
 * ``EXECUTE_ALL_RECOMMENDED`` response and as the ``INTERVENTION_APPLIED``
 * broadcast). Surfaces never re-derive success from raw results, so the
 * popup and the page panel cannot disagree about what happened.
 */

export type ApplyPhase = "idle" | "pending" | "applied" | "partial" | "failed";

export interface ApplyOutcome {
    phase: "applied" | "partial" | "failed";
    /** Actions that were applied and verified. */
    applied: number;
    /** Actions that were requested. */
    total: number;
    /** Consumer-facing reason when nothing (or not everything) changed. */
    reason: string | null;
}

export interface ApplyResultLike {
    action_id?: unknown;
    success?: unknown;
    message?: unknown;
}

/** How long an applied change stays undoable on a surface (at least). */
export const UNDO_WINDOW_MS = 60_000;

const KNOWN_REASONS: Array<[RegExp, string]> = [
    [/no longer active/i, "this suggestion has expired"],
    [/timed out/i, "Cortex didn't respond in time"],
    [/suggest-only mode/i, "workspace changes are off"],
    [/differs from the immutable manifest|absent from the manifest/i,
        "the suggestion changed before it could run"],
    [/not authorized/i, "Cortex didn't approve the change"],
    [/incognito/i, "Cortex never changes incognito windows"],
];

/** Translate an executor message into calm consumer language. */
export function humaniseApplyReason(raw: unknown): string {
    const text = typeof raw === "string" ? raw.trim() : "";
    if (!text) return "something went wrong";
    for (const [pattern, phrase] of KNOWN_REASONS) {
        if (pattern.test(text)) return phrase;
    }
    const compact = text.replace(/\.$/, "").replace(/^Error:\s*/i, "");
    // Mid-sentence phrasing, except that the product name keeps its case.
    const lowered = /^Cortex\b/.test(compact)
        ? compact
        : compact.charAt(0).toLowerCase() + compact.slice(1);
    return lowered.length > 80 ? `${lowered.slice(0, 77)}…` : lowered;
}

/**
 * Reduce raw executor results into the shared outcome.
 *
 * An error object (``{success:false, message}``), an empty list, or a
 * malformed payload all count as "nothing changed" — never as success.
 */
export function reduceApplyResults(
    raw: unknown,
    fallbackReason?: string,
): ApplyOutcome {
    const results: ApplyResultLike[] = Array.isArray(raw)
        ? raw.filter((item): item is ApplyResultLike =>
            typeof item === "object" && item !== null)
        : [];
    if (results.length === 0) {
        const errorMessage = !Array.isArray(raw)
            && typeof raw === "object"
            && raw !== null
            && typeof (raw as ApplyResultLike).message === "string"
            ? (raw as ApplyResultLike).message
            : undefined;
        return {
            phase: "failed",
            applied: 0,
            total: 0,
            reason: humaniseApplyReason(errorMessage ?? fallbackReason),
        };
    }
    const applied = results.filter((result) => result.success === true).length;
    const firstFailure = results.find((result) => result.success !== true);
    if (applied === results.length) {
        return { phase: "applied", applied, total: results.length, reason: null };
    }
    if (applied === 0) {
        return {
            phase: "failed",
            applied: 0,
            total: results.length,
            reason: humaniseApplyReason(firstFailure?.message ?? fallbackReason),
        };
    }
    return {
        phase: "partial",
        applied,
        total: results.length,
        reason: humaniseApplyReason(firstFailure?.message ?? fallbackReason),
    };
}

/** Label for the CTA once the outcome is known. */
export function applyStatusLabel(outcome: ApplyOutcome): string {
    if (outcome.phase === "applied") return "Applied";
    if (outcome.phase === "partial") {
        return `${outcome.applied} of ${outcome.total} applied`;
    }
    return `Nothing changed — ${outcome.reason ?? "something went wrong"}`;
}

/** Whether the surface should offer Undo for this outcome. */
export function outcomeIsUndoable(outcome: ApplyOutcome | null): boolean {
    return outcome !== null && outcome.phase !== "failed" && outcome.applied > 0;
}

export function isApplyOutcome(value: unknown): value is ApplyOutcome {
    if (typeof value !== "object" || value === null) return false;
    const candidate = value as Record<string, unknown>;
    return (candidate.phase === "applied"
            || candidate.phase === "partial"
            || candidate.phase === "failed")
        && Number.isInteger(candidate.applied)
        && Number.isInteger(candidate.total)
        && (candidate.reason === null || typeof candidate.reason === "string");
}
