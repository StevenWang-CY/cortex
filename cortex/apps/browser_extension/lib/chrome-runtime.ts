/**
 * Centralised chrome.runtime adapter helpers.
 *
 * The MV3 chrome.runtime API exposes ``lastError`` as an *intermittent*
 * property that the @types/chrome typings model as ``undefined |
 * { message?: string }`` even though the runtime guarantees a definite
 * read pattern (read inside the response callback before touching
 * ``response``). Callers that previously needed to interrogate
 * ``lastError`` reached for ``(chrome as unknown as {…}).runtime?.lastError``
 * — an unsafe double cast that this module eliminates by funnelling
 * every read through one place.
 *
 * F18 sweep: keep the unsafe cast in this single file so the rest of
 * the codebase can stay clean.
 */

export interface ChromeRuntimeError {
    message?: string;
}

/**
 * Return ``chrome.runtime.lastError`` if it is currently populated.
 *
 * The MV3 runtime only sets ``lastError`` for the duration of a
 * sendMessage / sendNativeMessage callback; outside that window it is
 * ``undefined``. Callers should read inside the callback before
 * touching the response argument.
 */
export function getLastRuntimeError(): ChromeRuntimeError | undefined {
    // Single cast — every other module imports from here.
    const r = (chrome as unknown as {
        runtime?: { lastError?: ChromeRuntimeError };
    }).runtime;
    return r?.lastError;
}

/**
 * Convenience boolean: true iff ``chrome.runtime.lastError`` is set.
 */
export function hasLastRuntimeError(): boolean {
    return getLastRuntimeError() !== undefined;
}
