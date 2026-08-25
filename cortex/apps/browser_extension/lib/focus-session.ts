/** Pure focus-session state transitions and distraction classification. */

export interface FocusSession {
    startTime: number;
    totalFocusMs: number;
    distractionsBlocked: number;
    lastFocusCheck: number;
    lastStateWasFocus: boolean;
    longestStreakMs: number;
    currentStreakStart: number;
    goal: string;
}

export interface FocusSessionSnapshot {
    elapsedMs: number;
    focusMs: number;
    focusPct: number;
    distractionsBlocked: number;
    longestStreakMin: number;
    goal: string;
    currentStreakMs: number;
}

export interface DailyStats {
    date: string;
    totalFocusMin: number;
    totalSessionMin: number;
    sessions: number;
    distractionsBlocked: number;
    longestStreakMin: number;
    avgHrDuringFocus: number;
    hrSamples: number;
}

const FOCUS_PRESET_DOMAINS: Readonly<Record<string, readonly RegExp[]>> = {
    developer: [
        /reddit\.com/i, /twitter\.com/i, /x\.com/i, /facebook\.com/i,
        /instagram\.com/i, /tiktok\.com/i, /youtube\.com/i, /netflix\.com/i,
        /9gag\.com/i, /buzzfeed\.com/i, /tumblr\.com/i, /twitch\.tv/i,
    ],
    student: [
        /instagram\.com/i, /tiktok\.com/i, /youtube\.com/i, /reddit\.com/i,
        /twitter\.com/i, /x\.com/i, /netflix\.com/i, /twitch\.tv/i,
        /facebook\.com/i, /snapchat\.com/i,
    ],
    writer: [
        /twitter\.com/i, /x\.com/i, /reddit\.com/i, /facebook\.com/i,
        /instagram\.com/i, /tiktok\.com/i, /youtube\.com/i, /netflix\.com/i,
        /hacker-news\.firebaseio\.com/i, /news\.ycombinator\.com/i,
    ],
    custom: [],
};

const ALWAYS_DISTRACTION = [
    /instagram\.com/i, /tiktok\.com/i, /netflix\.com/i,
    /twitch\.tv/i, /9gag\.com/i, /buzzfeed\.com/i, /tumblr\.com/i,
];
const CONDITIONAL_DISTRACTION = [
    /reddit\.com/i, /twitter\.com/i, /x\.com/i, /facebook\.com/i,
];
const AI_ASSISTANT_URL_PATTERN = /gemini\.google\.com|chatgpt\.com|chat\.openai\.com|claude\.ai|copilot\.microsoft\.com|perplexity\.ai/i;
const VIDEO_PLATFORM_URL_PATTERN = /youtube\.com|youtu\.be/i;
const TECH_SHORT_WORDS = new Set([
    "go", "ml", "ai", "css", "sql", "vue", "rx", "aws", "gcp", "api",
    "cli", "gui", "dom", "npm", "pip", "git", "ux", "ui", "db",
    "os", "ci", "cd", "qa", "c++", "c#", "r", "dx", "io", "jwt",
]);

export function createFocusSession(goal: string, now = Date.now()): FocusSession {
    return {
        startTime: now,
        totalFocusMs: 0,
        distractionsBlocked: 0,
        lastFocusCheck: now,
        lastStateWasFocus: false,
        longestStreakMs: 0,
        currentStreakStart: 0,
        goal,
    };
}

export function updateFocusSessionState(
    session: FocusSession,
    payload: Record<string, unknown>,
    now = Date.now(),
): FocusSession {
    const elapsed = Math.max(0, now - session.lastFocusCheck);
    const state = payload.state;
    const isFocused = payload.status === "estimated"
        && (state === "FLOW" || state === "RECOVERY");
    if (isFocused) {
        session.totalFocusMs += elapsed;
        if (!session.lastStateWasFocus) session.currentStreakStart = now;
        const currentStreak = Math.max(0, now - session.currentStreakStart);
        session.longestStreakMs = Math.max(
            session.longestStreakMs,
            currentStreak,
        );
    } else {
        session.currentStreakStart = 0;
    }
    session.lastStateWasFocus = isFocused;
    session.lastFocusCheck = now;
    return session;
}

export function focusSessionSnapshot(
    session: FocusSession,
    now = Date.now(),
): FocusSessionSnapshot {
    const elapsed = Math.max(0, now - session.startTime);
    return {
        elapsedMs: elapsed,
        focusMs: session.totalFocusMs,
        focusPct: elapsed > 0
            ? Math.round((session.totalFocusMs / elapsed) * 100)
            : 0,
        distractionsBlocked: session.distractionsBlocked,
        longestStreakMin: Math.round(session.longestStreakMs / 60_000),
        goal: session.goal,
        currentStreakMs: session.lastStateWasFocus && session.currentStreakStart
            ? Math.max(0, now - session.currentStreakStart)
            : 0,
    };
}

export function resolveFocusPreset(name: string): RegExp[] {
    return [...(FOCUS_PRESET_DOMAINS[name] || FOCUS_PRESET_DOMAINS.developer)];
}

export function extractGoalKeywords(goal: string): string[] {
    return goal.toLowerCase().split(/\s+/).filter(
        (word) => word.length > 1 || TECH_SHORT_WORDS.has(word),
    );
}

export function isDistractionForSession(args: {
    url: string;
    title?: string;
    session: FocusSession | null;
    presetPatterns?: readonly RegExp[];
    customDomains?: readonly string[];
}): boolean {
    const {
        url,
        title,
        session,
        presetPatterns = [],
        customDomains = [],
    } = args;
    if (session) {
        if (presetPatterns.some((pattern) => pattern.test(url))) return true;
        const lowered = url.toLowerCase();
        if (customDomains.some((domain) => lowered.includes(domain.toLowerCase()))) {
            return true;
        }
    }
    if (ALWAYS_DISTRACTION.some((pattern) => pattern.test(url))) return true;
    if (AI_ASSISTANT_URL_PATTERN.test(url)) return false;

    const goalMatchesTitle = Boolean(
        session?.goal
        && title
        && extractGoalKeywords(session.goal).some((keyword) => (
            title.toLowerCase().includes(keyword)
        )),
    );
    if (VIDEO_PLATFORM_URL_PATTERN.test(url)) return !goalMatchesTitle;
    if (CONDITIONAL_DISTRACTION.some((pattern) => pattern.test(url))) {
        return !goalMatchesTitle;
    }
    return false;
}

export function emptyDailyStats(date: string): DailyStats {
    return {
        date,
        totalFocusMin: 0,
        totalSessionMin: 0,
        sessions: 0,
        distractionsBlocked: 0,
        longestStreakMin: 0,
        avgHrDuringFocus: 0,
        hrSamples: 0,
    };
}
