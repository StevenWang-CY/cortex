/**
 * Tokenised page toast (health nudges, break timers).
 *
 * Injected via ``chrome.scripting.executeScript``; the stylesheet arrives as
 * the ``css`` argument (see ``tokens.ts``). Runs in the page's isolated world:
 * no imports, no outer closures.
 */

export function injectCortexToast(title: string, body: string, css: string): void {
    const id = "cortex-toast";
    const previous = document.getElementById(id) as
        | (HTMLElement & { __cortexCleanup?: () => void })
        | null;
    previous?.__cortexCleanup?.();
    previous?.remove();

    const host = document.createElement("div") as HTMLElement & {
        __cortexCleanup?: () => void;
    };
    host.id = id;
    host.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;";
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = css + `
.cx-toast{position:relative;width:min(320px,calc(100vw - 32px));padding:12px 44px 12px 14px;border-radius:var(--cx-radius-window);pointer-events:auto;opacity:0;transform:translateY(-8px);transition:opacity var(--cx-duration-fast) var(--cx-ease-out),transform var(--cx-duration-fast) var(--cx-ease-out)}
.cx-toast[data-state="open"]{opacity:1;transform:none}
.cx-toast[data-state="exit"]{opacity:0;transform:translateY(-8px);transition-duration:var(--cx-duration-micro);transition-timing-function:cubic-bezier(.4,0,1,1)}
.cx-toast .cx-title{font-size:13px;padding-right:0}
.cx-toast .cx-body{font-size:12px}
@media (prefers-reduced-motion:reduce){.cx-toast{transition:opacity var(--cx-duration-fast) var(--cx-ease-out)!important;transform:none!important}}
`;
    const toast = document.createElement("div");
    toast.className = "cx-panel cx-toast";
    toast.setAttribute("data-state", "enter");
    const titleEl = document.createElement("div");
    titleEl.className = "cx-title";
    titleEl.textContent = title;
    const bodyEl = document.createElement("div");
    bodyEl.className = "cx-body";
    bodyEl.textContent = body;
    const close = document.createElement("button");
    close.className = "cx-close";
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss notification");
    close.innerHTML = '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M1 1l8 8M9 1l-8 8"/></svg>';
    toast.append(titleEl, bodyEl, close);
    shadow.append(style, toast);
    document.body.appendChild(host);

    // Commit the "enter" frame before switching to "open" so the transition
    // retargets from the hidden state instead of snapping.
    void toast.offsetWidth;
    toast.setAttribute("data-state", "open");

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let dismissed = false;
    let hideTimer = 0;
    let removeTimer = 0;
    const dismiss = () => {
        if (dismissed) return;
        dismissed = true;
        window.clearTimeout(hideTimer);
        if (reduced) {
            host.remove();
            return;
        }
        toast.setAttribute("data-state", "exit");
        removeTimer = window.setTimeout(() => host.remove(), 140);
    };
    close.addEventListener("click", dismiss);
    hideTimer = window.setTimeout(dismiss, 8000);
    host.__cortexCleanup = () => {
        window.clearTimeout(hideTimer);
        window.clearTimeout(removeTimer);
    };
}
