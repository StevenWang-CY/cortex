/** Optional host-permission boundary.
 *
 * Request functions must be called directly from a user gesture (the popup
 * button does this). Incognito tabs are excluded even if Chrome's extension
 * toggle later allows the extension in incognito mode.
 */

export const OPTIONAL_SITE_ORIGINS = ["https://*/*", "http://*/*"] as const;
const EXPLICIT_SITE_CONSENT_KEY = "cortex_page_context_origins";

export interface SiteAccessState {
    available: boolean;
    granted: boolean;
    origin: string | null;
    incognitoCollection: false;
}

function originPattern(rawUrl: string): string | null {
    try {
        const url = new URL(rawUrl);
        if (url.protocol !== "https:" && url.protocol !== "http:") return null;
        return `${url.origin}/*`;
    } catch {
        return null;
    }
}

async function explicitOrigins(): Promise<Set<string>> {
    try {
        const stored = await chrome.storage.local.get(EXPLICIT_SITE_CONSENT_KEY);
        const values = stored[EXPLICIT_SITE_CONSENT_KEY];
        if (!Array.isArray(values)) return new Set();
        return new Set(values.filter((value): value is string => (
            typeof value === "string" && originPattern(value) === value
        )));
    } catch {
        return new Set();
    }
}

async function rememberExplicitOrigin(origin: string, granted: boolean): Promise<void> {
    const origins = await explicitOrigins();
    if (granted) origins.add(origin);
    else origins.delete(origin);
    await chrome.storage.local.set({
        [EXPLICIT_SITE_CONSENT_KEY]: Array.from(origins).sort(),
    });
}

async function activeTab(): Promise<chrome.tabs.Tab | null> {
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        return tab ?? null;
    } catch {
        return null;
    }
}

export async function getSiteAccessState(): Promise<SiteAccessState> {
    const tab = await activeTab();
    const origin = tab?.url ? originPattern(tab.url) : null;
    if (!origin || tab?.incognito) {
        return {
            available: false,
            granted: false,
            origin,
            incognitoCollection: false,
        };
    }
    try {
        const [browserGranted, consented] = await Promise.all([
            chrome.permissions.contains({ origins: [origin] }),
            explicitOrigins().then((origins) => origins.has(origin)),
        ]);
        return {
            available: true,
            granted: browserGranted && consented,
            origin,
            incognitoCollection: false,
        };
    } catch {
        return {
            available: true,
            granted: false,
            origin,
            incognitoCollection: false,
        };
    }
}

/** Request one exact origin. Call directly from a user gesture. */
export async function requestSiteAccess(origin: string): Promise<boolean> {
    if (originPattern(origin) !== origin) return false;
    const browserGranted = await chrome.permissions.request({ origins: [origin] });
    if (!browserGranted) return false;
    await rememberExplicitOrigin(origin, true);
    return true;
}

/** Revoke Cortex consent even when a required content-script host is not removable. */
export async function revokeSiteAccess(origin: string): Promise<boolean> {
    if (originPattern(origin) !== origin) return false;
    await rememberExplicitOrigin(origin, false);
    try {
        await chrome.permissions.remove({ origins: [origin] });
    } catch {
        // Required content-script hosts cannot always be removed. The explicit
        // consent record remains authoritative and has already been burned.
    }
    return true;
}

export async function mayExtractPageContent(
    tab: Pick<chrome.tabs.Tab, "url" | "incognito">,
): Promise<boolean> {
    if (tab.incognito || !tab.url) return false;
    const origin = originPattern(tab.url);
    if (!origin) return false;
    try {
        const consented = (await explicitOrigins()).has(origin);
        if (!consented) return false;
        return await chrome.permissions.contains({ origins: [origin] });
    } catch {
        return false;
    }
}
