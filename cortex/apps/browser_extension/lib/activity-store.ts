/** Durable, privacy-minimized activity repository for the MV3 worker. */

import {
    sanitizeActivityRecord,
    type ActivityRecord,
} from "./activity-privacy";
import { mayExtractPageContent } from "./site-access";

const ACTIVITY_STORAGE_KEY = "cortex_activities";
const MAX_ACTIVITIES = 200;
const ACTIVITY_SYNC_INTERVAL = 60_000;

export interface ActivitySyncRecord {
    content_id: string;
    platform: string;
    content_type: string;
    title: string;
    url: string;
    position_description: string;
    duration_spent_s: number;
    last_visited: number;
    completion_pct: number;
    topic_tags: string[];
    context_snapshot: string;
}

interface ActivityStoreBindings {
    isConnected: () => boolean;
    sync: (activities: ActivitySyncRecord[]) => void;
}

let bindings: ActivityStoreBindings = {
    isConnected: () => false,
    sync: () => undefined,
};
let lastActivitySyncTime = 0;

export function configureActivityStore(next: ActivityStoreBindings): void {
    bindings = next;
}

export async function loadActivities(): Promise<Record<string, ActivityRecord>> {
    const data = await chrome.storage.local.get(ACTIVITY_STORAGE_KEY);
    const raw = data[ACTIVITY_STORAGE_KEY];
    return raw && typeof raw === "object"
        ? raw as Record<string, ActivityRecord>
        : {};
}

export async function saveActivities(
    activities: Record<string, ActivityRecord>,
): Promise<void> {
    await chrome.storage.local.set({ [ACTIVITY_STORAGE_KEY]: activities });
}

export async function scrubStoredActivityContent(): Promise<void> {
    const activities = await loadActivities();
    const scrubbed: Record<string, ActivityRecord> = {};
    for (const record of Object.values(activities)) {
        const safe = sanitizeActivityRecord(record, false);
        if (safe) scrubbed[safe.content_id] = safe;
    }
    await saveActivities(scrubbed);
}

export async function upsertActivity(record: ActivityRecord): Promise<void> {
    const activities = await loadActivities();
    const existing = activities[record.content_id];

    if (existing) {
        const isSameSession = existing.first_visited === record.first_visited
            || record.first_visited > existing.last_visited - 10_000;
        if (isSameSession) {
            existing.duration_spent_s = (
                existing.duration_spent_s
                - existing.session_duration_s
                + record.duration_spent_s
            );
            existing.session_duration_s = record.duration_spent_s;
        } else {
            existing.duration_spent_s += record.duration_spent_s;
            existing.session_duration_s = record.duration_spent_s;
            existing.visit_count += 1;
            existing.dismissed = false;
        }
        existing.position = record.position;
        existing.last_visited = record.last_visited;
        existing.context_snapshot = record.context_snapshot;
        if (record.cognitive_state) existing.cognitive_state = record.cognitive_state;
        existing.completion_pct = Math.max(
            existing.completion_pct,
            record.completion_pct,
        );
        existing.max_completion_pct = Math.max(
            existing.max_completion_pct,
            record.completion_pct,
        );
        existing.related_tabs = Array.from(new Set([
            ...existing.related_tabs,
            ...record.related_tabs,
        ])).slice(0, 5);
        if (record.title) existing.title = record.title;
        activities[record.content_id] = existing;
    } else {
        activities[record.content_id] = record;
    }

    const entries = Object.entries(activities);
    if (entries.length > MAX_ACTIVITIES) {
        entries.sort((left, right) => left[1].last_visited - right[1].last_visited);
        while (Object.keys(activities).length > MAX_ACTIVITIES) {
            const oldest = entries.shift();
            if (!oldest) break;
            delete activities[oldest[0]];
        }
    }
    await saveActivities(activities);

    const now = Date.now();
    if (
        bindings.isConnected()
        && now - lastActivitySyncTime > ACTIVITY_SYNC_INTERVAL
    ) {
        lastActivitySyncTime = now;
        bindings.sync(buildActivitySyncPayload(activities));
    }
}

export async function prepareActivityRecordForStorage(
    raw: unknown,
    senderTab: Pick<chrome.tabs.Tab, "url" | "incognito"> | undefined,
): Promise<ActivityRecord | null> {
    if (!senderTab || senderTab.incognito) return null;
    const allowPageContent = await mayExtractPageContent(senderTab);
    return sanitizeActivityRecord(raw, allowPageContent);
}

export function buildActivitySyncPayload(
    activities: Record<string, ActivityRecord>,
): ActivitySyncRecord[] {
    return Object.values(activities)
        .sort((left, right) => right.last_visited - left.last_visited)
        .slice(0, 10)
        .map((activity) => ({
            content_id: activity.content_id,
            platform: activity.platform,
            content_type: activity.content_type,
            title: activity.title,
            url: activity.url,
            position_description: formatPositionDescription(activity),
            duration_spent_s: activity.duration_spent_s,
            last_visited: activity.last_visited,
            completion_pct: activity.completion_pct,
            topic_tags: activity.topic_tags,
            context_snapshot: activity.context_snapshot,
        }));
}

export function canonicalizeActivityUrl(rawUrl: string): string {
    let url: URL;
    try {
        url = new URL(rawUrl);
    } catch {
        return rawUrl;
    }
    const stripped = [
        "utm_source", "utm_medium", "utm_campaign", "utm_term",
        "utm_content", "fbclid", "gclid", "ref", "source", "si",
        "feature", "pp",
    ];
    for (const parameter of stripped) url.searchParams.delete(parameter);
    url.hostname = url.hostname.replace(/^www\./, "");

    if (url.hostname.includes("youtube.com") || url.hostname.includes("youtu.be")) {
        const video = url.searchParams.get("v");
        if (video) return `https://youtube.com/watch?v=${video}`;
        if (url.hostname === "youtu.be") {
            return `https://youtube.com/watch?v=${url.pathname.slice(1)}`;
        }
    }
    if (url.hostname.includes("bilibili.com")) {
        const match = url.pathname.match(/\/video\/(BV\w+)/);
        const page = url.searchParams.get("p") || "1";
        if (match) return `https://bilibili.com/video/${match[1]}?p=${page}`;
    }
    if (url.hostname.includes("leetcode")) {
        const match = url.pathname.match(/\/problems\/([^/]+)/);
        if (match) return `https://${url.hostname}/problems/${match[1]}`;
    }

    const keepHash = [/docs\.google\.com\/presentation/, /\.pdf$/i];
    if (!keepHash.some((pattern) => pattern.test(rawUrl))) url.hash = "";
    return url.toString();
}

export async function enrichWithRelatedTabs(record: ActivityRecord): Promise<void> {
    try {
        const tabs = (await chrome.tabs.query({})).filter((tab) => !tab.incognito);
        const activities = await loadActivities();
        const related: string[] = [];
        for (const tab of tabs) {
            if (!tab.url || tab.url === record.url) continue;
            const canonical = canonicalizeActivityUrl(tab.url);
            if (activities[canonical]) related.push(canonical);
        }
        record.related_tabs = related.slice(0, 5);
    } catch {
        // Tab enumeration is best-effort and cannot block persistence.
    }
}

function formatPositionDescription(activity: ActivityRecord): string {
    const position = activity.position;
    switch (position.type) {
        case "video":
            return `${formatTime(position.timestamp_s as number)} / ${
                formatTime(position.duration_s as number)
            }`;
        case "scroll":
            return `${Math.round(position.scroll_pct as number)}% read`;
        case "code_problem":
            return `Stage: ${position.stage} · ${position.wrong_answer_count} WA`;
        case "notebook":
            return `Cell ${(position.cell_index as number) + 1}`;
        case "pdf":
            return `Page ${position.page}/${position.total_pages}`;
        case "slides":
            return `Slide ${(position.slide_index as number) + 1}/${position.total_slides}`;
        case "general":
            return `${Math.round(position.scroll_pct as number)}% scrolled`;
        default:
            return "";
    }
}

function formatTime(seconds: number): string {
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const remainder = whole % 60;
    if (hours > 0) {
        return `${hours}:${String(minutes).padStart(2, "0")}:${
            String(remainder).padStart(2, "0")
        }`;
    }
    return `${minutes}:${String(remainder).padStart(2, "0")}`;
}
