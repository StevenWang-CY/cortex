/**
 * LeetCode coach — a corner variant of the shared panel.
 *
 * ``buildCoachPanelModel`` (worker) turns a ``LEETCODE_SHOW_*`` cue into
 * copy plus an honest hint ladder: hints come from ``payload.hints`` when the
 * daemon supplies them, otherwise from a three-rung ladder derived from the
 * problem tags. "Reveal next hint" indexes into that ladder and says which
 * rung it is on; it never repeats one sentence.
 *
 * ``injectCoachPanel`` runs in the page's isolated world (no imports, no
 * outer closures). The panel is a named region that receives focus on mount
 * unless the user is typing, closes on Escape only while it owns focus or
 * the pointer, and restores focus on dismiss.
 */

export interface CoachPanelModel {
    kind: string;
    title: string;
    body: string;
    tags: string[];
    hints: string[];
    /** Optional self-check the user can tick (submission gate). */
    check: string | null;
}

const MAX_HINTS = 5;

function stringArray(value: unknown, limit: number): string[] {
    return Array.isArray(value)
        ? value
            .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
            .map((item) => item.trim())
            .slice(0, limit)
        : [];
}

export function ladderFromTags(tags: readonly string[]): string[] {
    const family = tags[0] ?? "this";
    return [
        `Classify first: this reads like a ${family} problem. Say which sub-pattern applies before touching code.`,
        "Name the state you are tracking and one invariant that must hold after every step.",
        "Trace the smallest input that breaks a naive approach, then write the transition it demands.",
    ];
}

export function buildCoachPanelModel(
    kind: string,
    payload: Record<string, unknown>,
): CoachPanelModel {
    const tags = stringArray(payload.tags, 5);
    const suppliedHints = stringArray(payload.hints, MAX_HINTS);
    const problem = typeof payload.problem_title === "string" && payload.problem_title
        ? payload.problem_title
        : "this problem";

    let title = "Cortex coach";
    let body = "Pause briefly and make the next move explicit.";
    let hints: string[] = [];
    let check: string | null = null;

    switch (kind) {
        case "LEETCODE_SHOW_SCRATCHPAD":
            title = "Restate before solving";
            body = `In your own words: what is the input, what must come out, and what stays true throughout ${problem}?`;
            break;
        case "LEETCODE_SHOW_PATTERN_LADDER":
            title = "Pattern ladder";
            body = "Reveal only as much help as you need. Start with the category, not code.";
            hints = suppliedHints.length > 0 ? suppliedHints : ladderFromTags(tags);
            break;
        case "LEETCODE_SHOW_SUBMISSION_GATE": {
            const wrong = Number(payload.wrong_answer_count || 0);
            title = "Before the next submit";
            body = `${wrong} wrong answer${wrong === 1 ? "" : "s"} so far. Add one concrete failing test and trace it by hand first.`;
            check = "I traced one failing case by hand";
            break;
        }
        case "LEETCODE_SHOW_SOLUTION_FRICTION":
            title = "Before opening solutions";
            body = "Say what you expect the editorial’s key idea to be. A guess first keeps the solution useful instead of replacing the learning step.";
            break;
        case "LEETCODE_SHOW_CONSOLIDATION":
            title = "Consolidate the solve";
            body = "While it is fresh: what was the transferable pattern, and what would you recognise faster next time?";
            break;
        default:
            break;
    }

    return { kind, title, body, tags, hints, check };
}

/** Injected into the page. Self-contained by construction. */
export function injectCoachPanel(model: CoachPanelModel, css: string): void {
    const HOST_ID = "cortex-leetcode-coach";
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

    const tagsHtml = model.tags.length > 0
        ? `<div class="cx-tags" aria-label="Problem tags">${model.tags.map((tag) => `<span class="cx-tag">${esc(tag)}</span>`).join("")}</div>`
        : "";
    const hintsHtml = model.hints.length > 0
        ? `<div class="cx-actions cx-actions--stack"><button class="cx-btn cx-btn--ghost" id="reveal" type="button">Reveal hint 1 of ${model.hints.length}</button><div class="cx-hint" id="hint" role="status" aria-live="polite" hidden></div></div>`
        : "";
    const checkHtml = model.check
        ? `<label class="cx-check"><input id="check" type="checkbox"> <span>${esc(model.check)}</span></label>`
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
  <section class="cx-panel cx-panel--corner" id="panel" data-state="${isUpdate ? "open" : "enter"}" role="region" aria-labelledby="cortex-coach-title">
    <button class="cx-close" id="close" type="button" aria-label="Dismiss coach"><svg viewBox="0 0 10 10" aria-hidden="true"><path d="M1 1l8 8M9 1l-8 8"/></svg></button>
    <h2 class="cx-title" id="cortex-coach-title" tabindex="-1"><span class="cx-dot" aria-hidden="true"></span>${esc(model.title)}</h2>
    <div class="cx-body">${esc(model.body)}</div>
    ${tagsHtml}${checkHtml}${hintsHtml}
  </section>
</div>`;
    if (!existingHost) document.body.appendChild(host);

    const panel = shadow.getElementById("panel") as HTMLElement;
    const title = shadow.getElementById("cortex-coach-title") as HTMLElement;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (!isUpdate) {
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
    let removalTimer = 0;
    let revealed = 0;

    const dismiss = () => {
        if (closed) return;
        closed = true;
        document.removeEventListener("keydown", handleKeydown);
        const target = host.__cortexPreviousFocus;
        if (shadow.activeElement !== null && target?.isConnected) {
            try {
                target.focus({ preventScroll: true });
            } catch {
                target.focus();
            }
        }
        if (reducedMotion) {
            host.remove();
            return;
        }
        panel.setAttribute("data-state", "exit");
        removalTimer = window.setTimeout(() => host.remove(), 170);
    };

    const handleKeydown = (event: KeyboardEvent) => {
        if (event.key !== "Escape") return;
        if (shadow.activeElement === null && !hovered) return;
        event.preventDefault();
        dismiss();
    };

    panel.addEventListener("mouseenter", () => { hovered = true; });
    panel.addEventListener("mouseleave", () => { hovered = false; });
    document.addEventListener("keydown", handleKeydown);
    shadow.getElementById("close")?.addEventListener("click", dismiss);

    const reveal = shadow.getElementById("reveal") as HTMLButtonElement | null;
    const hint = shadow.getElementById("hint") as HTMLElement | null;
    reveal?.addEventListener("click", () => {
        if (!hint || revealed >= model.hints.length) return;
        hint.hidden = false;
        hint.textContent = `Hint ${revealed + 1} of ${model.hints.length}: ${model.hints[revealed]}`;
        revealed += 1;
        if (revealed >= model.hints.length) {
            reveal.disabled = true;
            reveal.textContent = "All hints shown";
        } else {
            reveal.textContent = `Reveal hint ${revealed + 1} of ${model.hints.length}`;
        }
    });

    host.__cortexCleanup = () => {
        window.clearTimeout(removalTimer);
        document.removeEventListener("keydown", handleKeydown);
    };
}
