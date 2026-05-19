/**
 * Runtime browser detection for the Cortex extension.
 *
 * The same bundled JS runs on both Chromium-family browsers we support
 * (Google Chrome and Microsoft Edge). Both expose the ``chrome.*`` API
 * surface, both load Plasmo's ``edge-mv3`` and ``chrome-mv3`` builds
 * identically at runtime, and the manifest "key" is shared so the
 * extension ID is stable across them — so the JS code itself cannot
 * tell which browser it is on without sniffing.
 *
 * The daemon's WebSocket IDENTIFY frame carries ``client_type``
 * (``chrome`` | ``edge`` | ``vscode`` | ``desktop``) which the desktop
 * dashboard's connection-dot row consumes. Misreporting Edge as Chrome
 * keeps the Edge dot dark forever and lights up the wrong indicator on
 * a multi-browser setup.
 *
 * Detection precedence (most specific first):
 *   1. ``userAgentData.brands[*].brand`` — UA-CH (User-Agent Client
 *      Hints). MV3 service workers expose ``navigator.userAgentData``
 *      on Chrome 90+ / Edge 90+. The Microsoft Edge brand is the
 *      definitive marker because Edge is the only Chromium derivative
 *      that ships it.
 *   2. ``navigator.userAgent`` fallback. Match ``Edg/`` (Edge ≥ 79) or
 *      legacy ``Edge/`` / ``EdgA/`` / ``EdgiOS/``. Older Edge versions
 *      we don't care about — Plasmo's edge-mv3 target requires Edge 88+
 *      anyway.
 *   3. Default to ``"chrome"``.
 *
 * Wrapped in ``try``/``catch`` so a sandboxed-context surprise never
 * crashes the IDENTIFY handshake. The fallback ``"chrome"`` is the
 * safer wrong answer than ``"unknown"`` (which the daemon rejects).
 */

export type CortexBrowser = "chrome" | "edge";

interface BrandedUAData {
    brands?: ReadonlyArray<{ brand: string; version: string }>;
}

interface NavigatorWithUAData {
    userAgentData?: BrandedUAData;
    userAgent?: string;
}

export function detectBrowser(nav?: NavigatorWithUAData): CortexBrowser {
    try {
        const n: NavigatorWithUAData =
            nav ??
            (typeof navigator !== "undefined"
                ? (navigator as unknown as NavigatorWithUAData)
                : {});

        const brands = n.userAgentData?.brands;
        if (Array.isArray(brands)) {
            for (const b of brands) {
                const brand = (b?.brand ?? "").toLowerCase();
                if (brand.includes("microsoft edge")) {
                    return "edge";
                }
            }
        }

        const ua = n.userAgent ?? "";
        if (/\bEdg(?:e|A|iOS)?\//.test(ua)) {
            return "edge";
        }
    } catch {
        // Sandboxed / minimal SW context — fall through to default.
    }
    return "chrome";
}
