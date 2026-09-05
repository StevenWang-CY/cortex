/**
 * Self-contained overlay removal for ``chrome.scripting.executeScript``.
 *
 * Every Cortex page surface parks a ``__cortexCleanup`` on its host element
 * so timers and document listeners can be torn down without re-running the
 * surface's own code. This function is injected on restore, on the daemon's
 * ``DISMISS_OVERLAY`` cue, on the ``Cmd+Shift+D`` command, and after a popup
 * dismissal, so an overlay can never outlive its intervention.
 *
 * Runs in the page's isolated world: no imports, no outer closures.
 */

export const CORTEX_SURFACE_HOST_IDS = [
    "cortex-somatic-overlay",
    "cortex-leetcode-coach",
    "cortex-distraction-interceptor",
] as const;

export function removeCortexOverlay(): boolean {
    const ids = [
        "cortex-somatic-overlay",
        "cortex-leetcode-coach",
        "cortex-distraction-interceptor",
    ];
    let removed = false;
    for (const id of ids) {
        const host = document.getElementById(id) as
            | (HTMLElement & { __cortexCleanup?: () => void })
            | null;
        if (!host) continue;
        try {
            host.__cortexCleanup?.();
        } catch {
            // A broken cleanup must not keep the host on the page.
        }
        host.remove();
        removed = true;
    }
    return removed;
}
