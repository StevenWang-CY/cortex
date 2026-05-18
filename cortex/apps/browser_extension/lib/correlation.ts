/**
 * F19b: Correlation IDs in the browser extension.
 *
 * Every UI-initiated action mints a 12-hex-char correlation id at the
 * popup/newtab boundary, threads it through every chrome.runtime
 * sendMessage round trip, and stamps every outbound WS send so a single
 * user click can be traced end-to-end across the popup, background
 * service worker, native host, and daemon logs.
 *
 * The id format is `cid_<12 hex chars>`. We prefer
 * `crypto.getRandomValues` when available (jsdom + Chrome both ship
 * it); fall back to `Math.random` so the helper is safe in any test
 * environment.
 */

const CID_PREFIX = "cid_";
const CID_LEN = 12;

function getCryptoSource(): Crypto | null {
    const g = globalThis as unknown as { crypto?: Crypto };
    if (g.crypto && typeof g.crypto.getRandomValues === "function") {
        return g.crypto;
    }
    return null;
}

export function newCorrelationId(): string {
    const src = getCryptoSource();
    let hex = "";
    if (src) {
        const buf = new Uint8Array(6); // 12 hex chars = 6 bytes
        src.getRandomValues(buf);
        hex = Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
    } else {
        // Math.random fallback (test contexts without crypto)
        for (let i = 0; i < CID_LEN; i++) {
            hex += Math.floor(Math.random() * 16).toString(16);
        }
    }
    return `${CID_PREFIX}${hex.slice(0, CID_LEN)}`;
}

export function isCorrelationId(value: unknown): value is string {
    return (
        typeof value === "string" &&
        value.startsWith(CID_PREFIX) &&
        value.length === CID_PREFIX.length + CID_LEN
    );
}

/**
 * Wraps a chrome.runtime onMessage-style handler so the cid carried on
 * the inbound payload is logged on receive (and can be propagated on
 * the response). The handler keeps its original signature.
 */
export function withCorrelationId<
    T extends (msg: Record<string, unknown>, ...rest: unknown[]) => unknown,
>(handler: T): T {
    const wrapped = ((msg: Record<string, unknown>, ...rest: unknown[]) => {
        const cid = typeof msg?.correlation_id === "string" ? msg.correlation_id : null;
        if (cid) {
            // Tag log lines uniformly so they grep cleanly: cid=cid_xxxxxxxxxxxx
            console.debug(`cortex.ext.recv cid=${cid} type=${String(msg?.type)}`);
        }
        return handler(msg, ...rest);
    }) as T;
    return wrapped;
}
