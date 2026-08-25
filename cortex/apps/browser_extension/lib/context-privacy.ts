/** Local minimisation shared by browser context collectors.
 *
 * This is defence in depth for the extension → localhost hop. The daemon's
 * context broker repeats and owns the authoritative external-send policy.
 */

export const PAGE_EXCERPT_MAX_CHARS = 2_000;
export const TAB_TITLE_MAX_CHARS = 160;

const BIDI_AND_ZERO_WIDTH = /[\u061c\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]/gu;
const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/gu;
const POSIX_PATH = /(^|[\s('"=:])\/(?:Users|home|private|Volumes|opt|var|tmp)\/[^\s'"<>|]+/giu;
const WINDOWS_PATH = /(^|[\s('"=:])(?:[a-z]:\\|\\\\)[^\s'"<>|]+/giu;

const SECRET_PATTERNS: ReadonlyArray<[string, RegExp]> = [
    ["PRIVATE_KEY", /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)/gu],
    ["AWS_KEY", /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/gu],
    ["GITHUB_TOKEN", /\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b/gu],
    ["SLACK_TOKEN", /\bxox(?:a|b|p|r|s)-[A-Za-z0-9-]{10,}\b/gu],
    ["ANTHROPIC_KEY", /\bsk-ant-[A-Za-z0-9_-]{16,}\b/gu],
    ["API_KEY", /\bsk-[A-Za-z0-9_-]{20,}\b/gu],
    ["JWT", /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/gu],
];
const GENERIC_SECRET = /\b(api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|client[_-]?secret|password|passwd|secret|token)(\s*[=:]\s*|\s+)(["']?)[^\s"',;]{8,}["']?/giu;
const URI_CREDENTIALS = /([a-z][a-z0-9+.-]*:\/\/)[^/@\s:]+:[^/@\s]+@/giu;

function basename(raw: string): string {
    const normalized = raw.replace(/\\/gu, "/").replace(/\/+$/u, "");
    return normalized.slice(normalized.lastIndexOf("/") + 1);
}

export interface SanitizedContextText {
    value: string;
    redactionCount: number;
}

export function sanitizeContextText(
    raw: string,
    maxChars: number,
): SanitizedContextText {
    if (!Number.isInteger(maxChars) || maxChars < 0) {
        throw new RangeError("maxChars must be a non-negative integer");
    }
    const scanLimit = maxChars + 4_096;
    let value = (raw ?? "")
        .slice(0, scanLimit)
        .normalize("NFKC")
        .replace(BIDI_AND_ZERO_WIDTH, "")
        .replace(CONTROL, " ");
    value = value.replace(POSIX_PATH, (match, prefix: string) => {
        const path = match.slice(prefix.length);
        return `${prefix}…/${basename(path)}`;
    });
    value = value.replace(WINDOWS_PATH, (match, prefix: string) => {
        const path = match.slice(prefix.length);
        return `${prefix}…\\${basename(path)}`;
    });

    let redactionCount = 0;
    value = value.replace(URI_CREDENTIALS, (_match, scheme: string) => {
        redactionCount += 1;
        return `${scheme}[REDACTED:URI_CREDENTIALS]@`;
    });
    for (const [name, pattern] of SECRET_PATTERNS) {
        value = value.replace(pattern, () => {
            redactionCount += 1;
            return `[REDACTED:${name}]`;
        });
    }
    value = value.replace(
        GENERIC_SECRET,
        (match, name: string, separator: string) => {
            if (match.includes("[REDACTED:")) return match;
            redactionCount += 1;
            return `${name}${separator}[REDACTED:SECRET]`;
        },
    );
    return { value: value.slice(0, maxChars), redactionCount };
}

/** Reduce an HTTP(S) URL to its origin. No userinfo/path/query/fragment. */
export function minimizeContextUrl(raw: string): string {
    try {
        const url = new URL(raw);
        if (url.protocol !== "https:" && url.protocol !== "http:") {
            return "[URL OMITTED]";
        }
        return url.origin;
    } catch {
        return "[URL OMITTED]";
    }
}
