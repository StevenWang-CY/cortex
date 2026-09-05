/**
 * Distraction interceptor — the modal variant of the shared panel, shown
 * over a distracting site during a focus session.
 *
 * Blocking surface contract: labelled dialog semantics, initial focus on the
 * safe choice ("Go back"), Tab containment between the two choices, Escape
 * equals "Go back", and focus restoration when the page is revealed. A fresh
 * tab (no history to go back to) asks the worker to close it instead of
 * leaving a blank page behind.
 *
 * Injected via ``chrome.scripting.executeScript``: no imports, no outer
 * closures; the stylesheet arrives as ``css``.
 */

export interface InterceptorModel {
    focusMin: number;
    streakMin: number;
    distractionsBlocked: number;
    domain: string;
}

export function injectDistractionInterceptor(
    model: InterceptorModel,
    css: string,
): void {
    const HOST_ID = "cortex-distraction-interceptor";
    type ManagedHost = HTMLElement & {
        __cortexCleanup?: () => void;
        __cortexPreviousFocus?: HTMLElement | null;
    };
    const existingHost = document.getElementById(HOST_ID) as ManagedHost | null;
    existingHost?.__cortexCleanup?.();
    existingHost?.remove();
    const esc = (value: string) => value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");

    const host = document.createElement("div") as ManagedHost;
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;inset:0;z-index:2147483647;";
    host.__cortexPreviousFocus = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const shadow = host.attachShadow({ mode: "open" });
    const plural = (count: number, unit: string) => `${count} ${unit}${count === 1 ? "" : "s"}`;
    shadow.innerHTML = `<style>${css}</style>
<div class="cx-layer" style="pointer-events:auto">
  <div class="cx-scrim" id="scrim" data-state="enter"></div>
  <div class="cx-center">
    <section class="cx-panel cx-panel--modal" id="panel" data-state="enter" role="dialog" aria-modal="true" aria-labelledby="cortex-interceptor-title" aria-describedby="cortex-interceptor-body">
      <h2 class="cx-title" id="cortex-interceptor-title">Focus session active</h2>
      <div class="cx-hero" aria-hidden="true">${model.focusMin}<span style="font-size:15px;font-weight:500"> min</span></div>
      <div class="cx-body" id="cortex-interceptor-body"><strong>${plural(model.focusMin, "minute")}</strong> steady, <strong>${plural(model.streakMin, "minute")}</strong> best streak. <strong>${esc(model.domain)}</strong> will break your flow.</div>
      <div class="cx-actions">
        <button class="cx-btn cx-btn--primary" id="back" type="button" style="width:auto;min-width:120px">Go back</button>
        <button class="cx-btn cx-btn--quiet" id="continue" type="button">Continue anyway</button>
      </div>
      <div class="cx-status cx-meta">${model.distractionsBlocked} blocked this session</div>
    </section>
  </div>
</div>`;
    document.body.appendChild(host);

    const panel = shadow.getElementById("panel") as HTMLElement;
    const scrim = shadow.getElementById("scrim") as HTMLElement;
    const back = shadow.getElementById("back") as HTMLButtonElement;
    const cont = shadow.getElementById("continue") as HTMLButtonElement;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    void panel.offsetWidth;
    panel.setAttribute("data-state", "open");
    scrim.setAttribute("data-state", "open");
    try {
        back.focus({ preventScroll: true });
    } catch {
        back.focus();
    }

    let closed = false;
    let removalTimer = 0;

    const notifyBackground = (message: Record<string, unknown>) => {
        try {
            chrome.runtime.sendMessage(message, () => {
                void chrome.runtime.lastError;
            });
        } catch {
            // Extension context gone; the page decision still applies.
        }
    };

    const remove = () => {
        if (closed) return;
        closed = true;
        document.removeEventListener("keydown", handleKeydown, true);
        const target = host.__cortexPreviousFocus;
        if (target?.isConnected) {
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
        scrim.setAttribute("data-state", "exit");
        removalTimer = window.setTimeout(() => host.remove(), 170);
    };

    const goBack = () => {
        if (closed) return;
        // A fresh tab has nowhere to go back to; ask the worker to close it
        // rather than leaving a blank page behind the interceptor.
        const freshTab = window.history.length <= 1;
        notifyBackground({
            type: "DISTRACTION_BLOCKED",
            leave: freshTab ? "close" : "back",
        });
        if (!freshTab) window.history.back();
        remove();
    };

    const handleKeydown = (event: KeyboardEvent) => {
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            goBack();
            return;
        }
        if (event.key === "Tab") {
            event.preventDefault();
            event.stopPropagation();
            const next = shadow.activeElement === back ? cont : back;
            next.focus({ preventScroll: true });
        }
    };

    back.addEventListener("click", goBack);
    cont.addEventListener("click", remove);
    document.addEventListener("keydown", handleKeydown, true);

    host.__cortexCleanup = () => {
        window.clearTimeout(removalTimer);
        document.removeEventListener("keydown", handleKeydown, true);
    };
}
