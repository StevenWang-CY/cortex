import { sanitizeContextText, TAB_TITLE_MAX_CHARS } from "./context-privacy";

export type ActivityContentType = "video" | "article" | "code_problem"
    | "documentation" | "course_lecture" | "notebook" | "pdf" | "slides"
    | "general";

export interface ActivityPosition {
    type: "video" | "scroll" | "code_problem" | "notebook" | "pdf" | "slides" | "general";
    [key: string]: unknown;
}

export interface ActivityRecord {
    content_id: string;
    platform: string;
    content_type: ActivityContentType;
    title: string;
    url: string;
    favicon_url: string;
    position: ActivityPosition;
    content_duration_s: number;
    duration_spent_s: number;
    session_duration_s: number;
    first_visited: number;
    last_visited: number;
    context_snapshot: string;
    topic_tags: string[];
    completion_pct: number;
    max_completion_pct: number;
    cognitive_state: string;
    visit_count: number;
    dismissed: boolean;
    is_playlist: boolean;
    playlist_id: string;
    playlist_index: number;
    related_tabs: string[];
}

const CONTENT_TYPES = new Set<ActivityContentType>([
    "video", "article", "code_problem", "documentation", "course_lecture",
    "notebook", "pdf", "slides", "general",
]);
const POSITION_TYPES = new Set<ActivityPosition["type"]>([
    "video", "scroll", "code_problem", "notebook", "pdf", "slides", "general",
]);
const PRIVATE_QUERY_NAME = /(?:^|_)(?:access|auth|code|credential|key|nonce|password|secret|session|signature|state|token)(?:_|$)/iu;
const TRACKING_QUERY_NAMES = new Set([
    "fbclid", "gclid", "ref", "si", "source", "feature", "pp",
    "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
]);

function finiteNumber(raw: unknown, min: number, max: number): number {
    if (typeof raw !== "number" || !Number.isFinite(raw)) return min;
    return Math.min(max, Math.max(min, raw));
}

function safeText(raw: unknown, maxChars: number): string {
    return sanitizeContextText(typeof raw === "string" ? raw : "", maxChars).value;
}

/** Keep resume-capable paths while stripping credentials, fragments, and risky query fields. */
export function sanitizeActivityUrl(raw: unknown): string | null {
    if (typeof raw !== "string") return null;
    try {
        const url = new URL(raw);
        if (url.protocol !== "https:" && url.protocol !== "http:") return null;
        url.username = "";
        url.password = "";
        url.hash = "";
        for (const name of Array.from(url.searchParams.keys())) {
            if (TRACKING_QUERY_NAMES.has(name.toLowerCase()) || PRIVATE_QUERY_NAME.test(name)) {
                url.searchParams.delete(name);
            }
        }
        const value = url.toString();
        return value.length <= 2_048 ? value : `${url.origin}${url.pathname}`.slice(0, 2_048);
    } catch {
        return null;
    }
}

function sanitizePosition(raw: unknown, allowPageContent: boolean): ActivityPosition | null {
    if (!raw || typeof raw !== "object") return null;
    const value = raw as Record<string, unknown>;
    if (typeof value.type !== "string" || !POSITION_TYPES.has(value.type as ActivityPosition["type"])) {
        return null;
    }
    switch (value.type) {
        case "video":
            return {
                type: "video",
                timestamp_s: finiteNumber(value.timestamp_s, 0, 31_536_000),
                duration_s: finiteNumber(value.duration_s, 0, 31_536_000),
                ...(allowPageContent && value.chapter
                    ? { chapter: safeText(value.chapter, 160) }
                    : {}),
            };
        case "scroll":
            return {
                type: "scroll",
                scroll_pct: finiteNumber(value.scroll_pct, 0, 100),
                scroll_px: finiteNumber(value.scroll_px, 0, 100_000_000),
                max_scroll_pct: finiteNumber(value.max_scroll_pct, 0, 100),
            };
        case "code_problem":
            return {
                type: "code_problem",
                stage: safeText(value.stage, 32),
                wrong_answer_count: finiteNumber(value.wrong_answer_count, 0, 100_000),
                accepted: value.accepted === true,
                time_elapsed_s: finiteNumber(value.time_elapsed_s, 0, 31_536_000),
                ...(allowPageContent && value.code_snapshot
                    ? { code_snapshot: safeText(value.code_snapshot, 2_000) }
                    : {}),
            };
        case "notebook":
            return {
                type: "notebook",
                cell_index: finiteNumber(value.cell_index, 0, 10_000_000),
                scroll_pct: finiteNumber(value.scroll_pct, 0, 100),
            };
        case "pdf":
            return {
                type: "pdf",
                page: finiteNumber(value.page, 0, 10_000_000),
                total_pages: finiteNumber(value.total_pages, 0, 10_000_000),
            };
        case "slides":
            return {
                type: "slides",
                slide_index: finiteNumber(value.slide_index, 0, 10_000_000),
                total_slides: finiteNumber(value.total_slides, 0, 10_000_000),
            };
        case "general":
            return {
                type: "general",
                scroll_pct: finiteNumber(value.scroll_pct, 0, 100),
                max_scroll_pct: finiteNumber(value.max_scroll_pct, 0, 100),
            };
    }
    return null;
}

/** Validate and minimise an untrusted content-script activity message. */
export function sanitizeActivityRecord(
    raw: unknown,
    allowPageContent: boolean,
): ActivityRecord | null {
    if (!raw || typeof raw !== "object") return null;
    const value = raw as Record<string, unknown>;
    const url = sanitizeActivityUrl(value.url);
    const contentId = sanitizeActivityUrl(value.content_id);
    const position = sanitizePosition(value.position, allowPageContent);
    if (
        !url
        || !contentId
        || !position
        || typeof value.content_type !== "string"
        || !CONTENT_TYPES.has(value.content_type as ActivityContentType)
    ) return null;

    const relatedTabs = Array.isArray(value.related_tabs)
        ? value.related_tabs
            .map(sanitizeActivityUrl)
            .filter((item): item is string => item !== null)
            .slice(0, 5)
        : [];
    const topicTags = Array.isArray(value.topic_tags)
        ? value.topic_tags
            .map((tag) => safeText(tag, 40))
            .filter(Boolean)
            .slice(0, 5)
        : [];

    return {
        content_id: contentId,
        platform: safeText(value.platform, 40),
        content_type: value.content_type as ActivityContentType,
        title: safeText(value.title, TAB_TITLE_MAX_CHARS),
        url,
        favicon_url: "",
        position,
        content_duration_s: finiteNumber(value.content_duration_s, 0, 31_536_000),
        duration_spent_s: finiteNumber(value.duration_spent_s, 0, 31_536_000),
        session_duration_s: finiteNumber(value.session_duration_s, 0, 31_536_000),
        first_visited: finiteNumber(value.first_visited, 0, Number.MAX_SAFE_INTEGER),
        last_visited: finiteNumber(value.last_visited, 0, Number.MAX_SAFE_INTEGER),
        context_snapshot: allowPageContent
            ? safeText(value.context_snapshot, 200)
            : "",
        topic_tags: topicTags,
        completion_pct: finiteNumber(value.completion_pct, 0, 100),
        max_completion_pct: finiteNumber(value.max_completion_pct, 0, 100),
        cognitive_state: safeText(value.cognitive_state, 40),
        visit_count: finiteNumber(value.visit_count, 1, 100_000),
        dismissed: value.dismissed === true,
        is_playlist: value.is_playlist === true,
        playlist_id: safeText(value.playlist_id, 160),
        playlist_index: finiteNumber(value.playlist_index, -1, 10_000_000),
        related_tabs: relatedTabs,
    };
}
