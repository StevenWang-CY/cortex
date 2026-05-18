/**
 * F07b + F08b: extension fetches the capability token from the native
 * host and caches it in `chrome.storage.session` so subsequent SHUTDOWN
 * messages and `/stop` fetches can present it without re-querying.
 *
 * The native host (cortex/scripts/native_host.py) responds to
 * `{command: "get_auth_token"}` with `{auth_token: "<hex>"}` —
 * see F07/F08 (already shipped server-side). This helper is the
 * extension counterpart that wires the cached token into the kill
 * chain on the browser side.
 */

const SESSION_KEY = "cortex_auth_token";
const NATIVE_APP = "com.cortex.launcher";

let inFlight: Promise<string> | null = null;

interface SessionGetResult {
    [key: string]: unknown;
}

async function readCachedToken(): Promise<string | null> {
    try {
        const data: SessionGetResult = await new Promise((resolve) => {
            chrome.storage.session.get(SESSION_KEY, (d) => resolve(d as SessionGetResult));
        });
        const tok = data[SESSION_KEY];
        return typeof tok === "string" && tok.length > 0 ? tok : null;
    } catch {
        return null;
    }
}

async function writeCachedToken(token: string): Promise<void> {
    try {
        await new Promise<void>((resolve) => {
            chrome.storage.session.set({ [SESSION_KEY]: token }, () => resolve());
        });
    } catch {
        // session storage may be unavailable in some service-worker
        // restart timing windows; cache miss is preferable to a crash.
    }
}

async function fetchFromNativeHost(): Promise<string> {
    return new Promise((resolve, reject) => {
        try {
            chrome.runtime.sendNativeMessage(
                NATIVE_APP,
                { command: "get_auth_token" },
                (resp: unknown) => {
                    const r = resp as { auth_token?: unknown; status?: string; error?: string } | undefined;
                    if (r && typeof r.auth_token === "string" && r.auth_token.length > 0) {
                        resolve(r.auth_token);
                        return;
                    }
                    reject(new Error(r?.error || "no_auth_token_in_response"));
                },
            );
        } catch (err) {
            reject(err instanceof Error ? err : new Error(String(err)));
        }
    });
}

/**
 * Returns the cached auth token, fetching it from the native host on
 * first need. Subsequent calls hit `chrome.storage.session`. A single
 * in-flight fetch is shared across concurrent callers so we don't fan
 * out N native-host requests on a cold start.
 */
export async function getAuthToken(): Promise<string> {
    const cached = await readCachedToken();
    if (cached) return cached;
    if (inFlight) return inFlight;
    inFlight = (async () => {
        try {
            const token = await fetchFromNativeHost();
            await writeCachedToken(token);
            return token;
        } finally {
            inFlight = null;
        }
    })();
    return inFlight;
}

/** Test-only: clear the cached token + reset the in-flight latch. */
export async function _resetAuthCache(): Promise<void> {
    inFlight = null;
    try {
        await new Promise<void>((resolve) => {
            chrome.storage.session.remove(SESSION_KEY, () => resolve());
        });
    } catch {
        // ignore
    }
}
