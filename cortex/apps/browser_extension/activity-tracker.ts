/**
 * Cortex — Universal Activity Tracker Content Script
 *
 * Tracks learning activity across ALL platforms: video (YouTube, Bilibili,
 * Coursera, edX, Khan Academy), code (LeetCode handled via bridge, HackerRank,
 * Codeforces), reading (MDN, docs, articles), notebooks (Jupyter, Colab),
 * PDFs, and slides (Google Slides, reveal.js).
 *
 * Sends ACTIVITY_UPDATE messages to the background script every 5s.
 * Lightweight: polls position, no heavy DOM observation.
 *
 * @plasmo content_script
 * @match <all_urls>
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ActivityPosition {
    type: "video" | "scroll" | "code_problem" | "notebook" | "pdf" | "slides" | "general";
    [key: string]: unknown;
}

interface VideoPosition extends ActivityPosition {
    type: "video";
    timestamp_s: number;
    duration_s: number;
    chapter?: string;
}

interface ScrollPosition extends ActivityPosition {
    type: "scroll";
    scroll_pct: number;
    scroll_px: number;
    max_scroll_pct: number;
}

interface CodeProblemPosition extends ActivityPosition {
    type: "code_problem";
    stage: string;
    wrong_answer_count: number;
    accepted: boolean;
    time_elapsed_s: number;
    code_snapshot?: string;
}

interface NotebookPosition extends ActivityPosition {
    type: "notebook";
    cell_index: number;
    scroll_pct: number;
}

interface PdfPosition extends ActivityPosition {
    type: "pdf";
    page: number;
    total_pages: number;
}

interface SlidesPosition extends ActivityPosition {
    type: "slides";
    slide_index: number;
    total_slides: number;
}

interface GeneralPosition extends ActivityPosition {
    type: "general";
    scroll_pct: number;
    max_scroll_pct: number;
}

interface ActivityRecord {
    content_id: string;
    platform: string;
    content_type: "video" | "article" | "code_problem" | "documentation"
        | "course_lecture" | "notebook" | "pdf" | "slides" | "general";
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

interface PlatformDetector {
    match(url: URL, hostname: string): boolean;
    platform: string;
    contentType: ActivityRecord["content_type"];
    getPosition(): ActivityPosition | null;
    getCompletionPct(): number;
    getTitle(): string;
    getContentDuration(): number;
    getPlaylistInfo(): { id: string; index: number } | null;
    isExcluded(): boolean;
}

// ---------------------------------------------------------------------------
// URL Canonicalization
// ---------------------------------------------------------------------------

const STRIP_PARAMS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "si", "feature", "pp",
];

function canonicalizeUrl(rawUrl: string): string {
    let u: URL;
    try {
        u = new URL(rawUrl);
    } catch {
        return rawUrl;
    }

    for (const p of STRIP_PARAMS) u.searchParams.delete(p);
    u.hostname = u.hostname.replace(/^www\./, "");

    // YouTube
    if (u.hostname.includes("youtube.com") || u.hostname.includes("youtu.be")) {
        const v = u.searchParams.get("v");
        if (v) return `https://youtube.com/watch?v=${v}`;
        if (u.hostname === "youtu.be") return `https://youtube.com/watch?v=${u.pathname.slice(1)}`;
    }

    // Bilibili
    if (u.hostname.includes("bilibili.com")) {
        const match = u.pathname.match(/\/video\/(BV\w+)/);
        const p = u.searchParams.get("p") || "1";
        if (match) return `https://bilibili.com/video/${match[1]}?p=${p}`;
    }

    // LeetCode
    if (u.hostname.includes("leetcode")) {
        const match = u.pathname.match(/\/problems\/([^/]+)/);
        if (match) return `https://${u.hostname}/problems/${match[1]}`;
    }

    // Strip hash for non-SPA sites (keep for PDFs, Google Slides)
    const KEEP_HASH_PATTERNS = [/docs\.google\.com\/presentation/, /\.pdf$/i];
    if (!KEEP_HASH_PATTERNS.some(p => p.test(rawUrl))) {
        u.hash = "";
    }

    return u.toString();
}

// ---------------------------------------------------------------------------
// Exclusion Checks
// ---------------------------------------------------------------------------

const EXCLUDED_URL_PREFIXES = ["chrome://", "chrome-extension://", "about:", "file://", "edge://"];
const EXCLUDED_URL_PATTERNS = [/\/login/i, /\/signin/i, /\/auth/i, /\/oauth/i];
const SEARCH_ENGINES = [/google\.\w+\/search/, /bing\.com\/search/, /duckduckgo\.com\//];
const NEWTAB_PATTERNS = [/chrome:\/\/newtab/, /edge:\/\/newtab/, /about:blank/];

function isExcludedUrl(url: string): boolean {
    if (EXCLUDED_URL_PREFIXES.some(p => url.startsWith(p))) return true;
    if (EXCLUDED_URL_PATTERNS.some(p => p.test(url))) return true;
    if (SEARCH_ENGINES.some(p => p.test(url))) return true;
    if (NEWTAB_PATTERNS.some(p => p.test(url))) return true;
    return false;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getScrollPct(): number {
    const scrollHeight = document.documentElement.scrollHeight - window.innerHeight;
    if (scrollHeight <= 0) return 0;
    return Math.min(100, (window.scrollY / scrollHeight) * 100);
}

function extractContextSnapshot(): string {
    const candidates = document.querySelectorAll("article, main, [role='main']");
    let el: Element | null = candidates[0] || null;
    if (!el) {
        // Find largest text block
        const blocks = document.querySelectorAll("p, div");
        let maxLen = 0;
        for (const b of blocks) {
            const text = b.textContent?.trim() || "";
            if (text.length > maxLen) { maxLen = text.length; el = b; }
        }
    }
    const text = el?.textContent?.trim() || document.title;
    return text.slice(0, 200);
}

function extractTopicTags(title: string, url: string): string[] {
    const tags: string[] = [];
    const combined = (title + " " + url).toLowerCase();
    const keywords = [
        "algorithm", "data structure", "react", "python", "javascript", "typescript",
        "machine learning", "deep learning", "database", "sql", "api", "css", "html",
        "docker", "kubernetes", "aws", "linux", "git", "testing", "security",
        "networking", "operating system", "compiler", "math", "calculus", "statistics",
        "physics", "chemistry", "biology", "economics", "finance",
    ];
    for (const kw of keywords) {
        if (combined.includes(kw)) tags.push(kw);
    }
    return tags.slice(0, 5);
}

function discoverVideoElement(): HTMLVideoElement | null {
    const selectors = [
        "video.html5-main-video",       // YouTube
        ".bpx-player-video-wrap video", // Bilibili
        "video[data-dashjs-player]",    // edX DASH player
        "video",                         // Generic fallback
    ];
    for (const sel of selectors) {
        const el = document.querySelector<HTMLVideoElement>(sel);
        if (el && el.readyState >= 0) return el;
    }
    return null;
}

function waitForVideo(callback: (v: HTMLVideoElement) => void, maxWaitMs = 15000): void {
    const existing = discoverVideoElement();
    if (existing) { callback(existing); return; }

    const observer = new MutationObserver(() => {
        const v = discoverVideoElement();
        if (v) { observer.disconnect(); clearTimeout(timeout); callback(v); }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    const timeout = setTimeout(() => observer.disconnect(), maxWaitMs);
}

// ---------------------------------------------------------------------------
// Platform Detectors
// ---------------------------------------------------------------------------

function createYouTubeDetector(): PlatformDetector {
    let video: HTMLVideoElement | null = null;
    return {
        platform: "youtube",
        contentType: "video",
        match(url: URL, hostname: string): boolean {
            return hostname.includes("youtube.com") || hostname === "youtu.be";
        },
        getPosition(): VideoPosition | null {
            video = discoverVideoElement();
            if (!video || !video.duration || !isFinite(video.duration)) return null;
            // Get chapter if available
            const chapterEl = document.querySelector(
                ".ytp-chapter-title-content, ytd-chapter-renderer .ytp-chapter-title-content"
            );
            const chapter = chapterEl?.textContent?.trim() || undefined;
            return {
                type: "video",
                timestamp_s: video.currentTime,
                duration_s: video.duration,
                chapter,
            };
        },
        getCompletionPct(): number {
            video = discoverVideoElement();
            if (!video?.duration || !isFinite(video.duration)) return 0;
            return (video.currentTime / video.duration) * 100;
        },
        getTitle(): string {
            return document.title.replace(/ - YouTube$/, "").trim();
        },
        getContentDuration(): number {
            video = discoverVideoElement();
            return video?.duration && isFinite(video.duration) ? video.duration : 0;
        },
        getPlaylistInfo(): { id: string; index: number } | null {
            const u = new URL(location.href);
            const list = u.searchParams.get("list");
            const idx = u.searchParams.get("index");
            if (list) return { id: list, index: idx ? parseInt(idx, 10) : 0 };
            return null;
        },
        isExcluded(): boolean {
            video = discoverVideoElement();
            // Short videos (<60s)
            if (video?.duration && isFinite(video.duration) && video.duration < 60) return true;
            // Live streams
            if (video?.duration === Infinity) return true;
            if (location.pathname.includes("/live")) return true;
            if (location.pathname.includes("/shorts")) return true;
            return false;
        },
    };
}

function createBilibiliDetector(): PlatformDetector {
    let video: HTMLVideoElement | null = null;
    return {
        platform: "bilibili",
        contentType: "video",
        match(_url: URL, hostname: string): boolean {
            return hostname.includes("bilibili.com");
        },
        getPosition(): VideoPosition | null {
            video = document.querySelector<HTMLVideoElement>(".bpx-player-video-wrap video") || discoverVideoElement();
            if (!video || !video.duration || !isFinite(video.duration)) return null;
            return { type: "video", timestamp_s: video.currentTime, duration_s: video.duration };
        },
        getCompletionPct(): number {
            video = document.querySelector<HTMLVideoElement>(".bpx-player-video-wrap video") || discoverVideoElement();
            if (!video?.duration || !isFinite(video.duration)) return 0;
            return (video.currentTime / video.duration) * 100;
        },
        getTitle(): string {
            return document.title.replace(/_哔哩哔哩.*$/, "").trim();
        },
        getContentDuration(): number {
            video = document.querySelector<HTMLVideoElement>(".bpx-player-video-wrap video") || discoverVideoElement();
            return video?.duration && isFinite(video.duration) ? video.duration : 0;
        },
        getPlaylistInfo(): { id: string; index: number } | null {
            const u = new URL(location.href);
            const p = u.searchParams.get("p");
            if (p && parseInt(p, 10) > 1) {
                const match = u.pathname.match(/\/video\/(BV\w+)/);
                if (match) return { id: match[1], index: parseInt(p, 10) - 1 };
            }
            return null;
        },
        isExcluded(): boolean {
            video = document.querySelector<HTMLVideoElement>(".bpx-player-video-wrap video") || discoverVideoElement();
            if (video?.duration && isFinite(video.duration) && video.duration < 60) return true;
            if (video?.duration === Infinity) return true;
            if (location.pathname.includes("/live/")) return true;
            return false;
        },
    };
}

function createGenericVideoDetector(): PlatformDetector {
    // Covers Coursera, edX, Khan Academy, and any site with <video>
    let video: HTMLVideoElement | null = null;
    const COURSE_HOSTNAMES = [
        "coursera.org", "edx.org", "courses.edx.org",
        "khanacademy.org", "udemy.com", "udacity.com",
    ];
    return {
        platform: "video_platform",
        contentType: "course_lecture",
        match(_url: URL, hostname: string): boolean {
            const h = hostname.replace(/^www\./, "");
            if (COURSE_HOSTNAMES.some(c => h.includes(c))) return true;
            // Only match if a video element exists and it's not a known platform
            if (h.includes("youtube.com") || h.includes("youtu.be") || h.includes("bilibili.com")) return false;
            video = discoverVideoElement();
            return video !== null && video.duration > 60;
        },
        getPosition(): VideoPosition | null {
            video = discoverVideoElement();
            if (!video || !video.duration || !isFinite(video.duration)) return null;
            return { type: "video", timestamp_s: video.currentTime, duration_s: video.duration };
        },
        getCompletionPct(): number {
            video = discoverVideoElement();
            if (!video?.duration || !isFinite(video.duration)) return 0;
            return (video.currentTime / video.duration) * 100;
        },
        getTitle(): string {
            return document.title.trim();
        },
        getContentDuration(): number {
            video = discoverVideoElement();
            return video?.duration && isFinite(video.duration) ? video.duration : 0;
        },
        getPlaylistInfo(): null { return null; },
        isExcluded(): boolean {
            video = discoverVideoElement();
            if (video?.duration && isFinite(video.duration) && video.duration < 60) return true;
            if (video?.duration === Infinity) return true;
            return false;
        },
    };
}

function createHackerRankDetector(): PlatformDetector {
    return {
        platform: "hackerrank",
        contentType: "code_problem",
        match(_url: URL, hostname: string): boolean {
            return hostname.includes("hackerrank.com") && location.pathname.includes("/challenges/");
        },
        getPosition(): CodeProblemPosition | null {
            // Detect code editor
            const monaco = document.querySelector(".monaco-editor");
            const cm = document.querySelector(".CodeMirror, .cm-editor");
            if (!monaco && !cm) return null;

            let code = "";
            if (monaco) {
                const lines = monaco.querySelectorAll(".view-line");
                code = Array.from(lines).map(l => l.textContent || "").join("\n").slice(0, 2000);
            } else if (cm) {
                const cmInstance = (cm as any).CodeMirror;
                if (cmInstance) code = cmInstance.getValue().slice(0, 2000);
            }

            return {
                type: "code_problem",
                stage: "IMPLEMENT",
                wrong_answer_count: 0,
                accepted: false,
                time_elapsed_s: 0,
                code_snapshot: code || undefined,
            };
        },
        getCompletionPct(): number { return 0; },
        getTitle(): string {
            const h1 = document.querySelector("h1.challenge-page-label, h2.challenge-name");
            return h1?.textContent?.trim() || document.title.trim();
        },
        getContentDuration(): number { return 0; },
        getPlaylistInfo(): null { return null; },
        isExcluded(): boolean { return false; },
    };
}

function createPdfDetector(): PlatformDetector {
    return {
        platform: "pdf",
        contentType: "pdf",
        match(url: URL): boolean {
            if (url.pathname.toLowerCase().endsWith(".pdf")) return true;
            if (document.querySelector('embed[type="application/pdf"]')) return true;
            if (typeof (window as any).PDFViewerApplication !== "undefined") return true;
            return false;
        },
        getPosition(): PdfPosition | null {
            const pdfApp = (window as any).PDFViewerApplication;
            if (pdfApp && pdfApp.page && pdfApp.pagesCount) {
                return { type: "pdf", page: pdfApp.page, total_pages: pdfApp.pagesCount };
            }
            // Chrome built-in viewer: parse URL hash
            const pageMatch = location.hash.match(/#page=(\d+)/);
            if (pageMatch) {
                return { type: "pdf", page: parseInt(pageMatch[1], 10), total_pages: 0 };
            }
            return null;
        },
        getCompletionPct(): number {
            const pos = this.getPosition() as PdfPosition | null;
            if (pos && pos.total_pages > 0) return (pos.page / pos.total_pages) * 100;
            return 0;
        },
        getTitle(): string {
            return document.title.replace(/\.pdf$/i, "").trim();
        },
        getContentDuration(): number { return 0; },
        getPlaylistInfo(): null { return null; },
        isExcluded(): boolean { return false; },
    };
}

function createNotebookDetector(): PlatformDetector {
    return {
        platform: "notebook",
        contentType: "notebook",
        match(_url: URL, hostname: string): boolean {
            if (hostname.includes("colab.research.google.com")) return true;
            if (hostname.includes("localhost") && (location.pathname.includes("/notebooks/") || location.pathname.includes("/lab/"))) return true;
            return false;
        },
        getPosition(): NotebookPosition | null {
            // Colab cells
            let cells = document.querySelectorAll("colab-cell");
            if (cells.length > 0) {
                const focused = document.querySelector("colab-cell[focused], .cell.focused");
                const idx = focused ? Array.from(cells).indexOf(focused) : 0;
                return { type: "notebook", cell_index: Math.max(0, idx), scroll_pct: getScrollPct() };
            }
            // Jupyter Classic
            cells = document.querySelectorAll(".cell");
            if (cells.length > 0) {
                const selected = document.querySelector(".cell.selected");
                const idx = selected ? Array.from(cells).indexOf(selected) : 0;
                return { type: "notebook", cell_index: Math.max(0, idx), scroll_pct: getScrollPct() };
            }
            // JupyterLab
            cells = document.querySelectorAll(".jp-Cell");
            if (cells.length > 0) {
                return { type: "notebook", cell_index: 0, scroll_pct: getScrollPct() };
            }
            return null;
        },
        getCompletionPct(): number { return getScrollPct(); },
        getTitle(): string { return document.title.trim(); },
        getContentDuration(): number { return 0; },
        getPlaylistInfo(): null { return null; },
        isExcluded(): boolean { return false; },
    };
}

function createSlidesDetector(): PlatformDetector {
    return {
        platform: "slides",
        contentType: "slides",
        match(_url: URL, hostname: string): boolean {
            if (hostname.includes("docs.google.com") && location.pathname.includes("/presentation")) return true;
            if (document.querySelector(".reveal")) return true;
            return false;
        },
        getPosition(): SlidesPosition | null {
            // reveal.js
            const reveal = (window as any).Reveal;
            if (reveal && typeof reveal.getIndices === "function") {
                const indices = reveal.getIndices();
                const total = reveal.getTotalSlides?.() || 0;
                return { type: "slides", slide_index: indices.h, total_slides: total };
            }
            // Google Slides
            const slideMatch = location.hash.match(/#slide=id\.p(\d+)/);
            if (slideMatch) {
                return { type: "slides", slide_index: parseInt(slideMatch[1], 10), total_slides: 0 };
            }
            // Count filmstrip thumbnails for total
            const filmstrip = document.querySelectorAll("[data-slide-id]");
            if (filmstrip.length > 0) {
                return { type: "slides", slide_index: 0, total_slides: filmstrip.length };
            }
            return null;
        },
        getCompletionPct(): number {
            const pos = this.getPosition() as SlidesPosition | null;
            if (pos && pos.total_slides > 0) return ((pos.slide_index + 1) / pos.total_slides) * 100;
            return 0;
        },
        getTitle(): string { return document.title.replace(/ - Google Slides$/, "").trim(); },
        getContentDuration(): number { return 0; },
        getPlaylistInfo(): null { return null; },
        isExcluded(): boolean { return false; },
    };
}

function createScrollDetector(): PlatformDetector {
    // Generic scroll-based reading (MDN, docs, articles, blogs)
    let maxScrollPct = 0;
    return {
        platform: "article",
        contentType: "article",
        match(): boolean {
            // Fallback detector — always matches if nothing else did
            return true;
        },
        getPosition(): ScrollPosition {
            const pct = getScrollPct();
            maxScrollPct = Math.max(maxScrollPct, pct);
            return {
                type: "scroll",
                scroll_pct: pct,
                scroll_px: window.scrollY,
                max_scroll_pct: maxScrollPct,
            };
        },
        getCompletionPct(): number {
            return maxScrollPct;
        },
        getTitle(): string { return document.title.trim(); },
        getContentDuration(): number { return 0; },
        getPlaylistInfo(): null { return null; },
        isExcluded(): boolean {
            // Exclude pages with very little content
            return document.documentElement.scrollHeight <= window.innerHeight * 1.2;
        },
    };
}

// ---------------------------------------------------------------------------
// Platform Registry
// ---------------------------------------------------------------------------

const DETECTORS: PlatformDetector[] = [
    createYouTubeDetector(),
    createBilibiliDetector(),
    createHackerRankDetector(),
    createPdfDetector(),
    createNotebookDetector(),
    createSlidesDetector(),
    createGenericVideoDetector(),
    createScrollDetector(),  // Fallback — must be last
];

// ---------------------------------------------------------------------------
// Activity Tracker Core
// ---------------------------------------------------------------------------

// Duplicate injection guard
if ((window as any).__cortex_activity_tracker__) {
    // Already running in this context
} else {
    (window as any).__cortex_activity_tracker__ = true;
    initActivityTracker();
}

function initActivityTracker(): void {
    const currentUrl = location.href;

    // Immediate exclusion check
    if (isExcludedUrl(currentUrl)) return;

    // LeetCode: handled entirely by leetcode-observer.ts → background bridge
    try {
        const u = new URL(currentUrl);
        if (u.hostname.includes("leetcode") && u.pathname.includes("/problems/")) return;
    } catch { /* proceed */ }

    // Incognito check — chrome.extension may not exist in all contexts
    try {
        if (chrome.extension?.inIncognitoContext) return;
    } catch { /* not in extension context, proceed */ }

    let activeDetector: PlatformDetector | null = null;
    let sessionStartTime = Date.now();
    let lastSaveTime = 0;
    let dwellTimeMs = 0;
    let lastTrackedUrl = location.href;
    let maxCompletionPct = 0;

    // Find matching detector
    function detectPlatform(): PlatformDetector | null {
        try {
            const url = new URL(location.href);
            const hostname = url.hostname.replace(/^www\./, "");
            for (const d of DETECTORS) {
                if (d.match(url, hostname)) return d;
            }
        } catch { /* invalid URL */ }
        return null;
    }

    activeDetector = detectPlatform();
    if (!activeDetector) return;

    // Check exclusion from the detector itself
    if (activeDetector.isExcluded()) return;

    function buildRecord(): ActivityRecord | null {
        if (!activeDetector) return null;
        const position = activeDetector.getPosition();
        if (!position) return null;

        const now = Date.now();
        const completionPct = activeDetector.getCompletionPct();
        maxCompletionPct = Math.max(maxCompletionPct, completionPct);

        const playlist = activeDetector.getPlaylistInfo();

        return {
            content_id: canonicalizeUrl(location.href),
            platform: activeDetector.platform,
            content_type: activeDetector.contentType,
            title: activeDetector.getTitle(),
            url: location.href,
            favicon_url: "",
            position,
            content_duration_s: activeDetector.getContentDuration(),
            duration_spent_s: dwellTimeMs / 1000,
            session_duration_s: (now - sessionStartTime) / 1000,
            first_visited: sessionStartTime,
            last_visited: now,
            context_snapshot: extractContextSnapshot(),
            topic_tags: extractTopicTags(activeDetector.getTitle(), location.href),
            completion_pct: completionPct,
            max_completion_pct: maxCompletionPct,
            cognitive_state: "",
            visit_count: 1,
            dismissed: false,
            is_playlist: playlist !== null,
            playlist_id: playlist?.id || "",
            playlist_index: playlist?.index ?? -1,
            related_tabs: [],
        };
    }

    function saveCurrentActivity(): void {
        const now = Date.now();
        // Debounce: don't save more than once per 3s
        if (now - lastSaveTime < 3000) return;

        const record = buildRecord();
        if (!record) return;

        // Don't save if we haven't spent enough time (< 5s in current save cycle)
        // But DO send the message — the background handles the dwell gate (120s for resume card)
        lastSaveTime = now;

        try {
            chrome.runtime.sendMessage({
                type: "ACTIVITY_UPDATE",
                record,
            }).catch(() => { /* extension context invalidated */ });
        } catch {
            // Extension context invalidated
        }
    }

    function resetForNewPage(): void {
        sessionStartTime = Date.now();
        dwellTimeMs = 0;
        maxCompletionPct = 0;
        lastSaveTime = 0;
        activeDetector = detectPlatform();
    }

    // --- Dwell time tracking ---
    let lastTickTime = Date.now();
    let pageVisible = !document.hidden;

    function tickDwell(): void {
        if (pageVisible) {
            const now = Date.now();
            dwellTimeMs += now - lastTickTime;
            lastTickTime = now;
        } else {
            lastTickTime = Date.now();
        }
    }

    // --- Periodic save (every 5s) ---
    const saveInterval = setInterval(() => {
        tickDwell();
        if (pageVisible && dwellTimeMs > 5000) {
            saveCurrentActivity();
        }
    }, 5000);

    // --- SPA Navigation Detection ---

    // 1. YouTube-specific events
    document.addEventListener("yt-navigate-start", () => {
        tickDwell();
        saveCurrentActivity();
    });
    document.addEventListener("yt-navigate-finish", () => {
        lastTrackedUrl = location.href;
        resetForNewPage();
    });

    // 2. Universal URL polling (catches all SPAs)
    const urlPollInterval = setInterval(() => {
        if (location.href !== lastTrackedUrl) {
            tickDwell();
            saveCurrentActivity();
            lastTrackedUrl = location.href;

            // Check if new URL is excluded
            if (isExcludedUrl(location.href)) {
                cleanup();
                return;
            }

            resetForNewPage();
        }
    }, 2000);

    // 3. Visibility change — save when tab goes hidden
    document.addEventListener("visibilitychange", () => {
        tickDwell();
        pageVisible = !document.hidden;
        if (document.hidden) {
            saveCurrentActivity();
        }
        lastTickTime = Date.now();
    });

    // 4. beforeunload — save on tab close / navigation
    window.addEventListener("beforeunload", () => {
        tickDwell();
        saveCurrentActivity();
    });

    // --- Cleanup ---
    function cleanup(): void {
        clearInterval(saveInterval);
        clearInterval(urlPollInterval);
    }
}

export {};
