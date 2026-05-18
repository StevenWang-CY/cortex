/**
 * Cortex — LeetCode DOM Observer Content Script
 *
 * A separate content script that observes LeetCode's DOM to extract
 * problem-solving context. Resilient to React/Next.js hydration cycles.
 *
 * Emits LEETCODE_CONTEXT_UPDATE messages at 1Hz to the background script.
 *
 * @plasmo content_script
 * @match https://leetcode.com/problems/*
 * @match https://leetcode.cn/problems/*
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Stage = "READ" | "PLAN" | "IMPLEMENT" | "DEBUG" | "REFLECT";

interface LeetCodeContextPayload {
  problem_id: string | null;
  title: string;
  difficulty: string;
  tags: string[];
  time_elapsed_s: number;
  submission_count: number;
  wrong_answer_count: number;
  last_submission_result: string | null;
  last_submission_ts: number | null;
  accepted: boolean;
  stage: Stage;
  code_snapshot: string;
  code_line_count: number;
  code_delete_ratio_60s: number;
  chars_per_min: number;
  reread_count: number;
  solutions_tab_attempted: boolean;
}

interface LeetCodeContextMessage {
  type: "LEETCODE_CONTEXT_UPDATE";
  payload: LeetCodeContextPayload;
}

// ---------------------------------------------------------------------------
// Selectors — resilient to LeetCode's evolving DOM
// ---------------------------------------------------------------------------

const SELECTORS = {
  // Problem metadata
  title: [
    "[data-cy='question-title']",
    ".text-title-large",
    "div[class*='title'] a",
    "h4[class*='title']",
  ],
  difficulty: [
    "div[class*='difficulty']",
    "div[diff]",
    "span[class*='difficulty']",
    // Color-coded badge fallback
    ".text-olive",   // Easy
    ".text-yellow",  // Medium
    ".text-pink",    // Hard
  ],
  tags: [
    "a[class*='topic-tag']",
    "a[href*='/tag/']",
    "div[class*='topic'] a",
    "span[class*='tag__']",
  ],

  // Editor content — Monaco (view-lines) or CodeMirror
  editorLines: [
    ".view-lines",
    ".CodeMirror-lines",
    ".cm-content",
  ],
  editorContainer: [
    ".monaco-editor",
    ".CodeMirror",
    ".cm-editor",
    "[data-cy='code-area']",
    "div[class*='editor']",
  ],

  // Submission result banners
  submissionResult: [
    "[data-cy='submission-result']",
    "div[class*='result']",
    "span[class*='status']",
    "[class*='submission']",
  ],

  // Problem description scroll container
  problemDescription: [
    "[data-cy='question-content']",
    "div[class*='description']",
    "div[class*='content__'] .elfjS",
    "div[class*='question-content']",
  ],

  // Solutions / Editorial tab
  solutionsTab: [
    "div[data-cy='solutions-tab']",
    "a[href*='/solutions']",
    "button:has(> span)",  // generic tab buttons
    "div[class*='tab']",
  ],
} as const;

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

/** Try multiple selectors in order, return the first match. */
function queryFirst(selectors: readonly string[]): Element | null {
  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      if (el) return el;
    } catch {
      // Invalid selector in this browser — skip
    }
  }
  return null;
}

/** Try multiple selectors, return all matches de-duped. */
function queryAllUnique(selectors: readonly string[]): Element[] {
  const seen = new Set<Element>();
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach((el) => seen.add(el));
    } catch {
      // skip
    }
  }
  return Array.from(seen);
}

/** Normalize submission result text to a canonical status string. */
function normalizeResult(raw: string): string | null {
  const t = raw.trim().toLowerCase();
  if (t.includes("accepted") && !t.includes("wrong")) return "Accepted";
  if (t.includes("wrong answer")) return "Wrong Answer";
  if (t.includes("runtime error")) return "Runtime Error";
  if (t.includes("time limit") || t.includes("tle")) return "TLE";
  if (t.includes("memory limit") || t.includes("mle")) return "MLE";
  if (t.includes("compile error") || t.includes("compilation")) return "Compile Error";
  return null;
}

// ---------------------------------------------------------------------------
// Rolling Window for Typing Telemetry
// ---------------------------------------------------------------------------

interface KeystrokeEvent {
  ts: number;
  kind: "insert" | "delete";
}

class RollingKeystrokeWindow {
  private events: KeystrokeEvent[] = [];
  private readonly windowMs: number;

  constructor(windowMs = 60_000) {
    this.windowMs = windowMs;
  }

  push(kind: "insert" | "delete"): void {
    const now = Date.now();
    this.events.push({ ts: now, kind });
    this.prune(now);
  }

  /** Chars per minute across the current window. */
  get charsPerMin(): number {
    const now = Date.now();
    this.prune(now);
    if (this.events.length === 0) return 0;
    const earliest = this.events[0].ts;
    const spanMin = Math.max((now - earliest) / 60_000, 1 / 60); // at least 1s
    return this.events.filter((e) => e.kind === "insert").length / spanMin;
  }

  /** Delete ratio: deletes / total keystrokes in the window. */
  get deleteRatio(): number {
    const now = Date.now();
    this.prune(now);
    if (this.events.length === 0) return 0;
    const deletes = this.events.filter((e) => e.kind === "delete").length;
    return deletes / this.events.length;
  }

  private prune(now: number): void {
    const cutoff = now - this.windowMs;
    // Binary-ish prune: drop all events older than the window
    while (this.events.length > 0 && this.events[0].ts < cutoff) {
      this.events.shift();
    }
  }
}

// ---------------------------------------------------------------------------
// LeetCodeObserver
// ---------------------------------------------------------------------------

export class LeetCodeObserver {
  // --- Timing ---
  private startTime = Date.now();
  private emitInterval: ReturnType<typeof setInterval> | null = null;
  private urlWatchInterval: ReturnType<typeof setInterval> | null = null;
  private observer: MutationObserver | null = null;
  private hydrationRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private scrollRetryTimers: ReturnType<typeof setTimeout>[] = [];
  private focusOutTimer: ReturnType<typeof setTimeout> | null = null;
  private scrollableEl: Element | null = null;

  // --- Problem metadata (cached, refreshed on mutation) ---
  private problemId: string | null = null;
  private title = "";
  private difficulty = "";
  private tags: string[] = [];

  // --- Submission tracking ---
  private submissionCount = 0;
  private wrongAnswerCount = 0;
  private lastSubmissionResult: string | null = null;
  private lastSubmissionTs: number | null = null;
  private accepted = false;
  private seenResultTexts = new Set<string>(); // de-dup hydration re-renders

  // --- Code telemetry ---
  private keystrokes = new RollingKeystrokeWindow(60_000);
  private lastCodeSnapshot = "";
  private codeLineCount = 0;

  // --- Behavioral signals ---
  private rereadCount = 0;
  private solutionsTabAttempted = false;
  private editorFocused = false;
  private lastDescriptionScrollTop = 0;
  private problemDescriptionEl: Element | null = null;

  // --- Stage inference ---
  private currentStage: Stage = "READ";

  constructor() {
    this.init();
  }

  // -----------------------------------------------------------------------
  // Lifecycle
  // -----------------------------------------------------------------------

  private init(): void {
    // LeetCode uses Next.js — the DOM may not be ready immediately.
    // We retry until we find the problem title (hydration signal).
    this.attemptHydrationReady();
  }

  private attemptHydrationReady(attempt = 0): void {
    const titleEl = queryFirst(SELECTORS.title);
    if (titleEl && titleEl.textContent?.trim()) {
      this.onReady();
      return;
    }

    if (attempt < 30) {
      // Retry every 500ms for up to 15 seconds
      this.hydrationRetryTimer = setTimeout(
        () => this.attemptHydrationReady(attempt + 1),
        500,
      );
    } else {
      // Give up waiting for hydration; start anyway with whatever is available
      this.onReady();
    }
  }

  private onReady(): void {
    this.extractProblemId();
    this.refreshMetadata();
    this.attachMutationObserver();
    this.attachEditorListeners();
    this.attachScrollListeners();
    this.attachSolutionsTabListener();

    // Emit at 1Hz
    this.emitInterval = setInterval(() => this.emit(), 1000);

    // Cleanup on navigation / unload
    window.addEventListener("beforeunload", () => this.destroy());

    // Handle SPA navigation (LeetCode uses Next.js client-side routing)
    this.watchForUrlChange();
  }

  destroy(): void {
    // Persist session state for Welcome Back feature
    this.saveSessionState();

    if (this.emitInterval !== null) clearInterval(this.emitInterval);
    if (this.urlWatchInterval !== null) clearInterval(this.urlWatchInterval);
    if (this.hydrationRetryTimer !== null) clearTimeout(this.hydrationRetryTimer);
    if (this.focusOutTimer !== null) clearTimeout(this.focusOutTimer);
    for (const t of this.scrollRetryTimers) clearTimeout(t);
    this.scrollRetryTimers = [];
    this.observer?.disconnect();
    // Remove event listeners to prevent memory leaks
    document.removeEventListener("focusin", this.onFocusIn);
    document.removeEventListener("focusout", this.onFocusOut);
    document.removeEventListener("click", this.onDocumentClick, { capture: true } as EventListenerOptions);
    if (this.scrollableEl) {
      this.scrollableEl.removeEventListener("scroll", this.onDescriptionScroll);
      this.scrollableEl = null;
    }
    this.emitInterval = null;
    this.urlWatchInterval = null;
    this.observer = null;
  }

  /**
   * Save session state to chrome.storage.local for Welcome Back feature.
   * Called on destroy (tab close / navigation away).
   */
  private saveSessionState(): void {
    if (!this.problemId || !this.title) return;
    const elapsedS = Math.round((Date.now() - this.startTime) / 1000);
    // Only save if user has spent meaningful time (>30s)
    if (elapsedS < 30) return;
    try {
      chrome.storage.local.set({
        cortex_leetcode_session: {
          problem_id: this.problemId,
          title: this.title,
          difficulty: this.difficulty,
          tags: this.tags,
          code_snapshot: this.lastCodeSnapshot,
          stage: this.currentStage,
          time_elapsed_s: elapsedS,
          wrong_answer_count: this.wrongAnswerCount,
          last_submission_result: this.lastSubmissionResult,
          accepted: this.accepted,
          saved_at: Date.now(),
        },
      });
    } catch {
      // Extension context may be invalidated
    }
  }

  // -----------------------------------------------------------------------
  // Problem ID extraction from URL
  // -----------------------------------------------------------------------

  private extractProblemId(): void {
    const match = window.location.pathname.match(/\/problems\/([^/]+)/);
    this.problemId = match ? match[1] : null;
  }

  // -----------------------------------------------------------------------
  // Metadata extraction
  // -----------------------------------------------------------------------

  private refreshMetadata(): void {
    // Title
    const titleEl = queryFirst(SELECTORS.title);
    if (titleEl) {
      this.title = titleEl.textContent?.trim() ?? "";
    }

    // Difficulty
    const diffEl = queryFirst(SELECTORS.difficulty);
    if (diffEl) {
      const text = diffEl.textContent?.trim().toLowerCase() ?? "";
      if (text.includes("easy")) this.difficulty = "Easy";
      else if (text.includes("medium")) this.difficulty = "Medium";
      else if (text.includes("hard")) this.difficulty = "Hard";
    }

    // Tags
    const tagEls = queryAllUnique(SELECTORS.tags);
    const newTags: string[] = [];
    for (const el of tagEls) {
      const tag = el.textContent?.trim();
      if (tag && tag.length > 0 && tag.length < 50) {
        newTags.push(tag);
      }
    }
    if (newTags.length > 0) {
      this.tags = [...new Set(newTags)];
    }
  }

  // -----------------------------------------------------------------------
  // Submission result detection
  // -----------------------------------------------------------------------

  private scanSubmissionResults(): void {
    const resultEls = queryAllUnique(SELECTORS.submissionResult);
    for (const el of resultEls) {
      const raw = el.textContent?.trim() ?? "";
      if (!raw) continue;

      // De-dup: React re-renders the same result node
      const fingerprint = raw.slice(0, 80);
      if (this.seenResultTexts.has(fingerprint)) continue;

      const result = normalizeResult(raw);
      if (!result) continue;

      this.seenResultTexts.add(fingerprint);
      this.submissionCount++;
      this.lastSubmissionResult = result;
      this.lastSubmissionTs = Date.now();

      if (result === "Wrong Answer" || result === "Runtime Error") {
        this.wrongAnswerCount++;
      }
      if (result === "Accepted") {
        this.accepted = true;
      }
    }
  }

  // -----------------------------------------------------------------------
  // Code snapshot extraction
  // -----------------------------------------------------------------------

  private extractCode(): string {
    // Strategy 1: Monaco editor (view-lines)
    const viewLines = queryFirst(SELECTORS.editorLines);
    if (viewLines) {
      const lines: string[] = [];
      viewLines.querySelectorAll(".view-line").forEach((line) => {
        lines.push(line.textContent ?? "");
      });
      if (lines.length > 0) return lines.join("\n");

      // CodeMirror fallback within the same element
      const cmLines: string[] = [];
      viewLines.querySelectorAll(".cm-line").forEach((line) => {
        cmLines.push(line.textContent ?? "");
      });
      if (cmLines.length > 0) return cmLines.join("\n");

      // Raw text content as last resort
      const raw = viewLines.textContent?.trim();
      if (raw) return raw;
    }

    // Strategy 2: CodeMirror instance API
    const cmEl = document.querySelector(".CodeMirror") as HTMLElement | null;
    if (cmEl) {
      const cmInstance = (cmEl as unknown as { CodeMirror?: { getValue(): string } })
        .CodeMirror;
      if (cmInstance) return cmInstance.getValue();
    }

    return "";
  }

  private updateCodeTelemetry(): void {
    const code = this.extractCode();
    const prev = this.lastCodeSnapshot;

    if (code !== prev) {
      // Estimate insertions vs deletions by character delta
      const lenDiff = code.length - prev.length;
      if (lenDiff > 0) {
        for (let i = 0; i < lenDiff; i++) this.keystrokes.push("insert");
      } else if (lenDiff < 0) {
        for (let i = 0; i < Math.abs(lenDiff); i++) this.keystrokes.push("delete");
      } else {
        // Same length but different content — likely a mix of insert+delete
        this.keystrokes.push("insert");
        this.keystrokes.push("delete");
      }
    }

    this.lastCodeSnapshot = code;
    this.codeLineCount = code ? code.split("\n").length : 0;
  }

  // -----------------------------------------------------------------------
  // Editor focus detection
  // -----------------------------------------------------------------------

  private attachEditorListeners(): void {
    // Use focusin/focusout on the document to detect editor focus
    document.addEventListener("focusin", this.onFocusIn);
    document.addEventListener("focusout", this.onFocusOut);
  }

  private onFocusIn = (e: FocusEvent): void => {
    const target = e.target as Element | null;
    if (!target) return;

    const editorContainer = queryFirst(SELECTORS.editorContainer);
    if (editorContainer?.contains(target)) {
      this.editorFocused = true;
    }
  };

  private onFocusOut = (_e: FocusEvent): void => {
    // Small delay so that focusin fires first if focus moves within editor
    if (this.focusOutTimer !== null) clearTimeout(this.focusOutTimer);
    this.focusOutTimer = setTimeout(() => {
      const active = document.activeElement;
      const editorContainer = queryFirst(SELECTORS.editorContainer);
      if (!editorContainer || !active || !editorContainer.contains(active)) {
        this.editorFocused = false;
      }
    }, 50);
  };

  // -----------------------------------------------------------------------
  // Scroll listeners — reread detection
  // -----------------------------------------------------------------------

  private attachScrollListeners(): void {
    // Observe scrolling on the problem description panel
    // LeetCode splits the page; the left panel has the problem description
    const tryAttach = () => {
      const descEl = queryFirst(SELECTORS.problemDescription);
      if (descEl) {
        this.problemDescriptionEl = descEl;
        // The scrollable container is often the parent
        const scrollable = this.findScrollableParent(descEl);
        if (scrollable) {
          this.scrollableEl = scrollable;
          scrollable.addEventListener("scroll", this.onDescriptionScroll, {
            passive: true,
          });
        }
        return true;
      }
      return false;
    };

    if (!tryAttach()) {
      // Retry after hydration (store timers for cleanup)
      this.scrollRetryTimers.push(setTimeout(() => tryAttach(), 2000));
      this.scrollRetryTimers.push(setTimeout(() => tryAttach(), 5000));
    }
  }

  private findScrollableParent(el: Element): Element | null {
    let current: Element | null = el;
    while (current) {
      const style = getComputedStyle(current);
      if (
        style.overflow === "auto" ||
        style.overflow === "scroll" ||
        style.overflowY === "auto" ||
        style.overflowY === "scroll"
      ) {
        return current;
      }
      current = current.parentElement;
    }
    // Fallback: the element itself
    return el;
  }

  private onDescriptionScroll = (e: Event): void => {
    const target = e.target as Element;
    const scrollTop = target.scrollTop;

    // Detect scrolling back UP toward the top (re-reading the problem)
    if (
      this.editorFocused &&
      scrollTop < this.lastDescriptionScrollTop - 100
    ) {
      this.rereadCount++;
    }

    this.lastDescriptionScrollTop = scrollTop;
  };

  // -----------------------------------------------------------------------
  // Solutions tab gating
  // -----------------------------------------------------------------------

  private attachSolutionsTabListener(): void {
    // Use event delegation on document for clicks
    document.addEventListener("click", this.onDocumentClick, { capture: true });
  }

  private onDocumentClick = (e: MouseEvent): void => {
    const target = e.target as Element | null;
    if (!target) return;

    // Walk up the DOM to find if this is a solutions/editorial tab click
    let current: Element | null = target;
    for (let depth = 0; depth < 5 && current; depth++) {
      const text = current.textContent?.trim().toLowerCase() ?? "";
      const href = current.getAttribute("href") ?? "";

      if (
        text === "solutions" ||
        text === "editorial" ||
        text === "solution" ||
        href.includes("/solutions") ||
        href.includes("/editorial")
      ) {
        this.solutionsTabAttempted = true;
        return;
      }
      current = current.parentElement;
    }
  };

  // -----------------------------------------------------------------------
  // MutationObserver — resilient to React hydration flushes
  // -----------------------------------------------------------------------

  private attachMutationObserver(): void {
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;

    this.observer = new MutationObserver((_mutations) => {
      // Debounce rapid hydration flushes — process at most once per 250ms
      if (debounceTimer !== null) return;
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        this.onDomMutation();
      }, 250);
    });

    this.observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  private onDomMutation(): void {
    this.refreshMetadata();
    this.scanSubmissionResults();
    this.updateCodeTelemetry();
  }

  // -----------------------------------------------------------------------
  // SPA navigation detection (Next.js client-side routing)
  // -----------------------------------------------------------------------

  private watchForUrlChange(): void {
    let lastUrl = window.location.href;

    // Use a periodic check since popstate doesn't fire for pushState
    this.urlWatchInterval = setInterval(() => {
      const currentUrl = window.location.href;
      if (currentUrl !== lastUrl) {
        lastUrl = currentUrl;
        this.onNavigate();
      }
    }, 1000);
  }

  private onNavigate(): void {
    // Check if we're still on a problem page
    if (!window.location.pathname.startsWith("/problems/")) {
      this.destroy();
      return;
    }

    // Save previous problem's session before resetting
    this.saveSessionState();

    // Reset state for the new problem
    this.extractProblemId();
    this.title = "";
    this.difficulty = "";
    this.tags = [];
    this.submissionCount = 0;
    this.wrongAnswerCount = 0;
    this.lastSubmissionResult = null;
    this.lastSubmissionTs = null;
    this.accepted = false;
    this.seenResultTexts.clear();
    this.lastCodeSnapshot = "";
    this.codeLineCount = 0;
    this.rereadCount = 0;
    this.solutionsTabAttempted = false;
    this.currentStage = "READ";
    this.startTime = Date.now();

    // Wait for hydration of the new problem
    this.attemptHydrationReady();
  }

  // -----------------------------------------------------------------------
  // Stage inference
  // -----------------------------------------------------------------------

  private inferStage(): Stage {
    const elapsedS = (Date.now() - this.startTime) / 1000;
    const cpm = this.keystrokes.charsPerMin;

    // Post-submission states take priority
    if (this.lastSubmissionResult) {
      if (this.accepted) {
        return "REFLECT";
      }
      if (
        this.lastSubmissionResult === "Wrong Answer" ||
        this.lastSubmissionResult === "Runtime Error"
      ) {
        return "DEBUG";
      }
    }

    // First 60 seconds — reading the problem
    if (elapsedS < 60) {
      return "READ";
    }

    // If the user is scrolling the problem description (not in editor)
    if (!this.editorFocused) {
      return "READ";
    }

    // Editor focused: distinguish PLAN vs IMPLEMENT by typing speed
    if (cpm < 20) {
      return "PLAN";
    }

    return "IMPLEMENT";
  }

  // -----------------------------------------------------------------------
  // Emit context update
  // -----------------------------------------------------------------------

  private emit(): void {
    this.updateCodeTelemetry();
    this.currentStage = this.inferStage();

    const elapsedS = Math.round((Date.now() - this.startTime) / 1000);

    const payload: LeetCodeContextPayload = {
      problem_id: this.problemId,
      title: this.title,
      difficulty: this.difficulty,
      tags: this.tags,
      time_elapsed_s: elapsedS,
      submission_count: this.submissionCount,
      wrong_answer_count: this.wrongAnswerCount,
      last_submission_result: this.lastSubmissionResult,
      last_submission_ts: this.lastSubmissionTs,
      accepted: this.accepted,
      stage: this.currentStage,
      code_snapshot: this.lastCodeSnapshot,
      code_line_count: this.codeLineCount,
      code_delete_ratio_60s: Math.round(this.keystrokes.deleteRatio * 1000) / 1000,
      chars_per_min: Math.round(this.keystrokes.charsPerMin * 10) / 10,
      reread_count: this.rereadCount,
      solutions_tab_attempted: this.solutionsTabAttempted,
    };

    const message: LeetCodeContextMessage = {
      type: "LEETCODE_CONTEXT_UPDATE",
      payload,
    };

    try {
      chrome.runtime.sendMessage(message);
    } catch {
      // Extension context invalidated (e.g., extension reloaded)
    }
  }
}

// ---------------------------------------------------------------------------
// Auto-start
// ---------------------------------------------------------------------------

if (window.location.hostname.includes("leetcode.com") || window.location.hostname.includes("leetcode.cn")) {
  // Guard against duplicate injection (Plasmo may re-inject on HMR)
  const GUARD_KEY = "__cortex_leetcode_observer__";
  const guardedWindow = window as unknown as Record<string, unknown>;
  if (!guardedWindow[GUARD_KEY]) {
    guardedWindow[GUARD_KEY] = new LeetCodeObserver();
  }
}
