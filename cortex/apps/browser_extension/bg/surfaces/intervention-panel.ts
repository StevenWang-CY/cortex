/**
 * Intervention panel — the corner surface injected on an
 * ``INTERVENTION_TRIGGER`` whose ``ui_plan`` asks for an overlay.
 *
 * Two halves:
 *
 * * ``buildInterventionPanelModel`` runs in the worker. It normalises the
 *   wire payload (``micro_steps`` are ``MicroStep`` objects on the wire,
 *   never strings), filters placeholder copy, and decides which controls the
 *   page may show. Everything the page renders is a plain serialisable
 *   model, so the injected function never touches the raw payload.
 * * ``injectInterventionPanel`` runs in the page's isolated world through
 *   ``chrome.scripting.executeScript``. It has no imports and no outer
 *   closures; the stylesheet is passed in as ``css``.
 *
 * Apply feedback follows the shared machine in ``lib/apply-state.ts``:
 * idle → pending → applied | partial | failed, with Undo available for at
 * least ``undoWindowMs`` after an applied or partial outcome.
 */

import { UNDO_WINDOW_MS } from "../../lib/apply-state";
import { normaliseMicroSteps } from "../../lib/popup-view-model";
import {
    cleanTabReason,
    hasRealErrorAnalysis,
    isSpecificStep,
} from "../../lib/reason-phrases";

export interface InterventionPanelStep {
    text: string;
    done: boolean;
}

export interface InterventionPanelTab {
    title: string;
    reason: string;
}

export interface InterventionPanelModel {
    interventionId: string;
    headline: string;
    summary: string;
    steps: InterventionPanelStep[];
    closeTabs: InterventionPanelTab[];
    keepCount: number;
    error: { rootCause: string; suggestedFix: string } | null;
    /** Primary control label; ``null`` when nothing may run. */
    ctaLabel: string | null;
    /** Explanatory note under the proposal; ``""`` when none. */
    note: string;
    undoWindowMs: number;
    autoHideMs: number;
}

/** Idle panels retire after this long, reporting ``expired`` (not dismissed). */
export const PANEL_AUTO_HIDE_MS = 5 * 60 * 1_000;

function recordArray(value: unknown): Array<Record<string, unknown>> {
    return Array.isArray(value)
        ? value.filter((item): item is Record<string, unknown> =>
            typeof item === "object" && item !== null)
        : [];
}

export function primaryActionLabel(
    executable: Array<Record<string, unknown>>,
): string {
    if (executable.length === 1) {
        const actionType = String(executable[0].action_type || "");
        if (actionType === "search_error") return "Search this error";
        if (actionType === "open_url") return "Open recommended page";
        if (actionType === "highlight_tab") return "Switch to recommended tab";
        return "Apply change";
    }
    return `Apply ${executable.length} changes`;
}

export function buildInterventionPanelModel(
    payload: Record<string, unknown>,
    executableActionIds: readonly string[],
    options: { undoWindowMs?: number; autoHideMs?: number } = {},
): InterventionPanelModel {
    const executableIds = new Set(executableActionIds);
    const actions = recordArray(payload.suggested_actions);
    const recommended = actions.filter((action) => action.category === "recommended");
    const executable = recommended.filter((action) =>
        typeof action.action_id === "string" && executableIds.has(action.action_id));
    const manualRecommended = recommended.length > executable.length;
    const canExecute = payload.execution_mode === "authorized"
        || payload.execution_mode === "research_autonomous";

    const tabRecs = payload.tab_recommendations as
        | { tabs?: unknown }
        | null
        | undefined;
    const tabs = recordArray(tabRecs?.tabs);
    const closeTabs = tabs
        .filter((tab) => tab.action === "close" || tab.action === "bookmark_and_close")
        .map((tab) => ({
            title: String(tab.tab_title || "Untitled"),
            reason: cleanTabReason(tab.reason),
        }));
    const keepCount = tabs.filter((tab) => tab.action === "keep").length;

    const errorAnalysis = payload.error_analysis as
        | Record<string, unknown>
        | null
        | undefined;
    const error = errorAnalysis && hasRealErrorAnalysis(errorAnalysis.root_cause)
        ? {
            rootCause: errorAnalysis.root_cause,
            suggestedFix: typeof errorAnalysis.suggested_fix === "string"
                ? errorAnalysis.suggested_fix
                : "",
        }
        : null;

    const steps = normaliseMicroSteps(payload.micro_steps)
        .filter((step) => isSpecificStep(step.text))
        .map((step) => ({ text: step.text, done: step.status === "done" }));

    const note = !canExecute && executable.length > 0
        ? "Suggestions only — workspace changes are off."
        : closeTabs.length > 0 || manualRecommended
            ? "Manual review — Cortex won’t close or regroup existing tabs automatically."
            : "";

    return {
        interventionId: String(payload.intervention_id || ""),
        headline: String(payload.headline || ""),
        summary: String(payload.situation_summary || ""),
        steps,
        closeTabs,
        keepCount,
        error,
        ctaLabel: canExecute && executable.length > 0
            ? primaryActionLabel(executable)
            : null,
        note,
        undoWindowMs: options.undoWindowMs ?? UNDO_WINDOW_MS,
        autoHideMs: options.autoHideMs ?? PANEL_AUTO_HIDE_MS,
    };
}

/**
 * Injected into the page. Self-contained by construction — do not reference
 * anything outside this function body.
 */
export function injectInterventionPanel(
    model: InterventionPanelModel,
    css: string,
): void {
    const HOST_ID = "cortex-somatic-overlay";
    type ManagedHost = HTMLElement & {
        __cortexCleanup?: () => void;
        __cortexPreviousFocus?: HTMLElement | null;
    };
    const existingHost = document.getElementById(HOST_ID) as ManagedHost | null;
    existingHost?.__cortexCleanup?.();
    const isUpdate = existingHost !== null;
    const esc = (value: string) => value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");

    const stepsHtml = model.steps.length > 0
        ? `<div class="cx-section" aria-label="Next steps">${model.steps.map((step) =>
            `<div class="cx-item${step.done ? " is-done" : ""}"><span class="cx-text">${esc(step.text)}</span></div>`).join("")}</div>`
        : "";
    const tabsHtml = model.closeTabs.length > 0
        ? `<div class="cx-section"><div class="cx-label">Review ${model.closeTabs.length} tab suggestion${model.closeTabs.length === 1 ? "" : "s"}</div>${model.closeTabs.map((tab) =>
            `<div class="cx-item"><span class="cx-text">${esc(tab.title)}${tab.reason ? `<span class="cx-sub">${esc(tab.reason)}</span>` : ""}</span></div>`).join("")}${model.keepCount > 0 ? `<div class="cx-keep">Keeping <b>${model.keepCount}</b> you need</div>` : ""}</div>`
        : "";
    const errorHtml = model.error
        ? `<div class="cx-error"><div class="cx-error-head">Error</div><div class="cx-error-text">${esc(model.error.rootCause)}</div>${model.error.suggestedFix ? `<pre class="cx-code">${esc(model.error.suggestedFix)}</pre>` : ""}</div>`
        : "";
    const noteHtml = model.note ? `<div class="cx-note">${esc(model.note)}</div>` : "";
    const ctaHtml = model.ctaLabel
        ? `<div class="cx-actions cx-actions--stack"><button class="cx-btn cx-btn--primary" id="cta" type="button" data-phase="idle">${esc(model.ctaLabel)}</button><div class="cx-status" id="status" role="status" aria-live="polite"></div><button class="cx-link" id="undo" type="button" hidden>Undo</button></div>`
        : "";

    const host = (existingHost ?? document.createElement("div")) as ManagedHost;
    if (!existingHost) {
        host.id = HOST_ID;
        host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
        host.__cortexPreviousFocus = document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null;
    }
    const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
    shadow.innerHTML = `<style>${css}</style>
<div class="cx-layer">
  <section class="cx-panel cx-panel--corner" id="panel" data-state="${isUpdate ? "open" : "enter"}" role="region" aria-labelledby="cortex-intervention-title" aria-describedby="cortex-intervention-summary">
    <button class="cx-close" id="close" type="button" aria-label="Dismiss suggestion"><svg viewBox="0 0 10 10" aria-hidden="true"><path d="M1 1l8 8M9 1l-8 8"/></svg></button>
    <h2 class="cx-title" id="cortex-intervention-title" tabindex="-1">${esc(model.headline)}</h2>
    <div class="cx-body" id="cortex-intervention-summary" aria-live="polite">${esc(model.summary)}</div>
    ${tabsHtml}${errorHtml}${stepsHtml}${noteHtml}${ctaHtml}
    <div class="cx-actions"><button class="cx-btn cx-btn--quiet" id="dismiss" type="button">Dismiss</button></div>
  </section>
</div>`;
    if (!existingHost) document.body.appendChild(host);

    const panel = shadow.getElementById("panel") as HTMLElement;
    const title = shadow.getElementById("cortex-intervention-title") as HTMLElement;
    const cta = shadow.getElementById("cta") as HTMLButtonElement | null;
    const status = shadow.getElementById("status") as HTMLElement | null;
    const undo = shadow.getElementById("undo") as HTMLButtonElement | null;
    const dismissButton = shadow.getElementById("dismiss") as HTMLButtonElement;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (!isUpdate) {
        // Commit the hidden frame, then retarget to open; a later update or
        // dismissal retargets the same transition instead of replaying it.
        void panel.offsetWidth;
        panel.setAttribute("data-state", "open");
        const active = document.activeElement as HTMLElement | null;
        const typing = active !== null && (
            active.tagName === "INPUT"
            || active.tagName === "TEXTAREA"
            || active.isContentEditable
        );
        const documentFocused = typeof document.hasFocus === "function"
            ? document.hasFocus()
            : true;
        if (documentFocused && !typing) {
            try {
                title.focus({ preventScroll: true });
            } catch {
                title.focus();
            }
        }
    }

    let hovered = false;
    let closed = false;
    let phase: "idle" | "pending" | "applied" | "partial" | "failed" | "restored" = "idle";
    let dismissAction: "dismissed" | "engaged" = "dismissed";
    let autoHideTimer = 0;
    let removalTimer = 0;

    const notifyBackground = (message: Record<string, unknown>) => {
        try {
            chrome.runtime.sendMessage(message, () => {
                // Read lastError so a torn-down worker does not log noise.
                void chrome.runtime.lastError;
            });
        } catch {
            // The extension context may be gone; the panel still closes.
        }
    };

    const restoreFocus = () => {
        const target = host.__cortexPreviousFocus;
        if (shadow.activeElement === null || !target?.isConnected) return;
        try {
            target.focus({ preventScroll: true });
        } catch {
            target.focus();
        }
    };

    const removePanel = () => {
        if (closed) return;
        closed = true;
        window.clearTimeout(autoHideTimer);
        document.removeEventListener("keydown", handleKeydown);
        restoreFocus();
        if (reducedMotion) {
            host.remove();
            return;
        }
        panel.setAttribute("data-state", "exit");
        removalTimer = window.setTimeout(() => host.remove(), 170);
    };

    const closeWith = (action: "dismissed" | "engaged" | "expired") => {
        if (closed) return;
        notifyBackground({
            type: "USER_ACTION",
            action,
            intervention_id: model.interventionId,
        });
        removePanel();
    };

    const handleKeydown = (event: KeyboardEvent) => {
        if (event.key !== "Escape") return;
        // Escape belongs to the page unless the panel owns focus or the
        // pointer — closing a site dialog must never count as a dismissal.
        if (shadow.activeElement === null && !hovered) return;
        event.preventDefault();
        closeWith(dismissAction);
    };

    panel.addEventListener("mouseenter", () => { hovered = true; });
    panel.addEventListener("mouseleave", () => { hovered = false; });
    document.addEventListener("keydown", handleKeydown);
    shadow.getElementById("close")?.addEventListener("click", () => closeWith(dismissAction));
    dismissButton.addEventListener("click", () => closeWith(dismissAction));
    autoHideTimer = window.setTimeout(() => closeWith("expired"), model.autoHideMs);

    const setStatus = (text: string) => {
        if (status) status.textContent = text;
    };

    if (cta) {
        cta.addEventListener("click", () => {
            if (phase !== "idle") return;
            phase = "pending";
            cta.disabled = true;
            cta.setAttribute("data-phase", "pending");
            cta.setAttribute("aria-busy", "true");
            cta.textContent = "Applying…";
            setStatus("");
            let settled = false;
            const settle = (raw: unknown) => {
                if (settled || closed) return;
                settled = true;
                cta.removeAttribute("aria-busy");
                const response = raw as {
                    outcome?: {
                        phase?: unknown;
                        applied?: unknown;
                        total?: unknown;
                        reason?: unknown;
                    };
                } | undefined;
                const outcome = response?.outcome;
                const nextPhase = outcome?.phase === "applied"
                    || outcome?.phase === "partial"
                    || outcome?.phase === "failed"
                    ? outcome.phase
                    : "failed";
                const applied = typeof outcome?.applied === "number" ? outcome.applied : 0;
                const total = typeof outcome?.total === "number" ? outcome.total : 0;
                const reason = typeof outcome?.reason === "string" && outcome.reason
                    ? outcome.reason
                    : "Cortex didn't respond";
                phase = nextPhase;
                cta.setAttribute("data-phase", nextPhase);
                cta.disabled = true;
                if (nextPhase === "failed") {
                    cta.textContent = `Nothing changed — ${reason}`;
                    setStatus("");
                    return;
                }
                cta.textContent = nextPhase === "applied"
                    ? "Applied"
                    : `${applied} of ${total} applied`;
                setStatus(`${cta.textContent}. Undo stays available for a minute.`);
                if (undo) undo.hidden = false;
                dismissAction = "engaged";
                dismissButton.textContent = "Done";
                // The change stays undoable for the whole window; if the
                // user walks away the panel hides without ending the
                // intervention.
                window.clearTimeout(autoHideTimer);
                autoHideTimer = window.setTimeout(removePanel, model.undoWindowMs);
            };
            try {
                chrome.runtime.sendMessage(
                    { type: "EXECUTE_ALL_RECOMMENDED", intervention_id: model.interventionId },
                    (raw: unknown) => {
                        if (chrome.runtime.lastError) {
                            settle({ outcome: { phase: "failed", reason: "Cortex didn't respond" } });
                            return;
                        }
                        settle(raw);
                    },
                );
            } catch {
                settle({ outcome: { phase: "failed", reason: "Cortex didn't respond" } });
            }
        });
    }

    if (undo && cta) {
        undo.addEventListener("click", () => {
            if (phase !== "applied" && phase !== "partial") return;
            undo.disabled = true;
            undo.textContent = "Undoing…";
            const finish = () => {
                if (closed) return;
                phase = "restored";
                cta.setAttribute("data-phase", "restored");
                cta.textContent = "Restored";
                undo.hidden = true;
                undo.disabled = false;
                undo.textContent = "Undo";
                setStatus("Changes undone.");
                dismissAction = "dismissed";
                dismissButton.textContent = "Dismiss";
            };
            try {
                chrome.runtime.sendMessage(
                    { type: "UNDO_ALL_RECENT", intervention_id: model.interventionId },
                    () => {
                        void chrome.runtime.lastError;
                        finish();
                    },
                );
            } catch {
                finish();
            }
        });
    }

    host.__cortexCleanup = () => {
        window.clearTimeout(autoHideTimer);
        window.clearTimeout(removalTimer);
        document.removeEventListener("keydown", handleKeydown);
    };
}
