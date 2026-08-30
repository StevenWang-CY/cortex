/**
 * F07b + F08b: extension fetches the capability token from the native
 * host and caches it in `chrome.storage.session` so subsequent SHUTDOWN
 * messages and `/stop` fetches can present it without re-querying.
 *
 * The native host (cortex/scripts/native_host.py) responds using the
 * Pydantic-generated native contract. Runtime decoding is strict so a
 * Python/TypeScript field-name drift fails at this boundary.
 */

import { sendNativeHostMessage } from "./native-messaging";

const SESSION_KEY = "cortex_auth_token";
let inFlight: Promise<string> | null = null;

interface SessionGetResult {
  [key: string]: unknown;
}

async function readCachedToken(): Promise<string | null> {
  try {
    const data: SessionGetResult = await new Promise((resolve) => {
      chrome.storage.session.get(SESSION_KEY, (d) =>
        resolve(d as SessionGetResult),
      );
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
  const response = await sendNativeHostMessage(
    { command: "get_auth_token" },
    { timeoutMs: 8_000 },
  );
  if (response.command !== "get_auth_token") {
    throw new Error("unexpected_native_host_response");
  }
  return response.auth_token;
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
