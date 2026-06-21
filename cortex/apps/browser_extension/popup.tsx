/**
 * Cortex Chrome Extension — Popup UI
 *
 * Design: Cortex Visual Identity Guide — dark, calm, Linear/Claude-inspired.
 * Inter + JetBrains Mono typography, indigo accent, 4px grid spacing.
 * No emoji glyphs in user-visible UI — text labels only. No motivational
 * copy. Sentence case everywhere. (Inline comments may still reference
 * the legacy thumbs-up / thumbs-down emoji for historical clarity.)
 */

import React, { useCallback, useEffect, useState } from "react";
import { CX, STATE_COLORS, STATE_LABELS, CX_KEYFRAMES } from "./design-tokens";
import { newCorrelationId } from "./lib/correlation";
import { getLastRuntimeError } from "./lib/chrome-runtime";
import { DAEMON_HTTP_URL } from "./config";
import { getAuthToken } from "./lib/auth";
import type {
    BreakRecommendation as BreakRecommendationSchema,
    CausalSignal as CausalSignalSchema,
    WhyDetail as WhyDetailSchema,
} from "./types/generated/cortex_schemas";

// P2-9: centralised debug flag for popup. Mirrors background.ts's DEBUG
// pattern. In production builds (NODE_ENV=production / Plasmo production
// build) all console.debug / console.warn calls in this file are silenced.
const CORTEX_DEBUG: boolean = (() => {
    try {
        const ime = (import.meta as unknown as { env?: Record<string, unknown> }).env;
        if (ime && ime.CORTEX_DEBUG === "true") return true;
    } catch { /* import.meta not available */ }
    try {
        if (typeof process !== "undefined" && process.env && process.env.CORTEX_DEBUG === "true") {
            return true;
        }
    } catch { /* process not available */ }
    return false;
})();

/**
 * F19b: every popup-initiated request mints a correlation id at the click
 * boundary. The background script logs the id on receive and stamps it
 * on the outbound WS frame so a single click can be traced through
 * `popup → bg → native_host → daemon`.
 */
function sendWithCid(
    msg: Record<string, unknown>,
    cb?: (resp: unknown) => void,
): string {
    const correlation_id = newCorrelationId();
    const enriched = { ...msg, correlation_id };
    if (CORTEX_DEBUG) {
        console.debug(
            `cortex.popup.send cid=${correlation_id} type=${String(msg.type)}`,
        );
    }
    safeSendMessage(enriched, cb);
    return correlation_id;
}

/**
 * Phase 4d Task B: every ``chrome.runtime.sendMessage`` callback in
 * MV3 must consult ``chrome.runtime.lastError`` or Chrome surfaces an
 * "Unchecked runtime.lastError" warning when the background SW was
 * evicted mid-call. ``safeSendMessage`` centralises the lastError
 * inspection and routes failures through the popup-wide error sink
 * (``__cortexLastErrorSink``) that the toast renderer subscribes to.
 *
 * Callers that don't care about the response pass no callback; we
 * still wrap so the lastError check fires.
 */
type RuntimeErrorSink = (msg: string) => void;
let __cortexLastErrorSink: RuntimeErrorSink | null = null;
export function __setLastErrorSink(fn: RuntimeErrorSink | null): void {
    __cortexLastErrorSink = fn;
}
export function safeSendMessage(
    msg: Record<string, unknown>,
    cb?: (resp: unknown) => void,
): void {
    try {
        chrome.runtime.sendMessage(msg, (response) => {
            // F18 (Phase-4 audit): the unsafe ``chrome as unknown as``
            // cast lives in exactly one helper (lib/chrome-runtime.ts)
            // so every other surface stays clean.
            const lastErr = getLastRuntimeError();
            if (lastErr) {
                if (CORTEX_DEBUG) {
                    console.warn(
                        "[cortex.popup] sendMessage",
                        String(msg.type ?? "?"),
                        lastErr.message,
                    );
                }
                if (__cortexLastErrorSink) {
                    __cortexLastErrorSink(
                        lastErr.message ?? "background unavailable",
                    );
                }
                return;
            }
            if (cb) cb(response);
        });
    } catch (err) {
        if (CORTEX_DEBUG) {
            console.warn(
                "[cortex.popup] sendMessage threw",
                String(msg.type ?? "?"),
                err,
            );
        }
        if (__cortexLastErrorSink) {
            __cortexLastErrorSink("background unavailable");
        }
    }
}

// Generated from Pydantic — Debt-1 closure (F42/F43/F44).
// Hand-written copies of these interfaces previously lived alongside
// the popup; they drifted from the Python side. The import is the
// only canonical source; CI fails if it goes stale.
import type {
    DailyBaseline,
    SessionReport,
    SessionRecap,
    SuggestedAction,
    TabRecommendation,
    TabRecommendations,
    TrendsResponse,
} from "./types/generated/cortex_schemas";

/**
 * P0 §3.3: 24-hour TTL for the cached session recap. After this window
 * the popup hides the recap card even if the daemon never explicitly
 * dismissed it, so stale recaps don't loiter forever in the UI.
 */
const RECAP_TTL_MS = 24 * 60 * 60 * 1000;

/**
 * P0 §3.2: if the cached "Last 7 days" trends payload is older than
 * this, mounting the popup nudges the background script to ask the
 * daemon for a fresh rollup. The background script's 30-minute timer
 * also keeps the cache warm; this is the on-demand belt-and-braces.
 */
const TRENDS_STALENESS_MS = 6 * 60 * 60 * 1000;

/**
 * Phase 4d Task H / §3.24: hard cap on the ``pending_feedback`` queue in
 * ``chrome.storage.local``. Every offline bug report adds one entry and
 * we only drain on popup mount, so a user who runs Cortex with the
 * daemon down for months could otherwise accumulate thousands of
 * entries. Most-recent N wins — older reports are silently dropped.
 */
const PENDING_FEEDBACK_MAX = 100;

const CortexLogo = () => (
    <svg width="22" height="22" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" style={{ flexShrink: 0 }}>
        <path d="M 51.8 12.2 A 28 28 0 1 0 51.8 51.8" fill="none" stroke="#1a1a1a" strokeWidth="6" strokeLinecap="round" />
        <path d="M 12 32 L 22 32 L 27 15 L 37 49 L 42 32 L 60 32" fill="none" stroke="#D97757" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="60" cy="32" r="3" fill="#D97757" />
    </svg>
);

// --- Types ---

interface Biometrics {
    heart_rate: number | null;
    hrv_rmssd: number | null;
    blink_rate: number | null;
    forward_lean: number | null;
}

interface CortexState {
    state: string;
    confidence: number;
    scores: Record<string, number>;
    signal_quality: Record<string, number>;
    dwell_seconds: number;
    biometrics?: Biometrics;
    // Phase-4 audit (F5/F16): the daemon stamps capture-pipeline and
    // session-store health flags onto the STATE_UPDATE envelope. The
    // popup mirrors them into the BPM/HRV status banner and the
    // warning strip at the top of the panel.
    capture?: {
        frames_flowing?: boolean;
        face_detected?: boolean;
        stale?: boolean;
    };
    store?: {
        degraded?: boolean;
    };
}

interface FocusSnapshot {
    elapsedMs: number;
    focusMs: number;
    focusPct: number;
    distractionsBlocked: number;
    longestStreakMin: number;
    currentStreakMs: number;
    goal: string;
    // P0 §3.10 / Phase-3 P0-N? + Audit-1.2 F3: daemon-armed focus
    // sessions carry an auto-arm flag + preset name so the popup
    // surfaces a distinct "Auto-armed · developer" pill instead of
    // the generic focus chip.
    autoArmed?: boolean;
    preset?: string;
    endsAt?: number | null;
}

interface DailyStats {
    date: string;
    totalFocusMin: number;
    totalSessionMin: number;
    sessions: number;
    distractionsBlocked: number;
    longestStreakMin: number;
}

interface MorningBriefing {
    summary: string;
    action_items: string[];
    left_off_at: string;
}

/**
 * F52: synthesize close_tab actions from tab_recommendations *only*
 * for tab_index values not already covered by suggested_actions, and
 * type the action with the generated ``SuggestedAction["action_type"]``
 * literal union so a future Pydantic-side rename surfaces as a
 * compile error here (Debt-1).
 *
 * Previously two bugs:
 *   - If any suggested_action with a close intent existed, we skipped
 *     synthesis entirely — dropping the close affordance for any
 *     *other* recommended tab.
 *   - When no suggested_action close existed, we synthesised one per
 *     closeable rec, which could duplicate the close button when the
 *     LLM emitted a partial suggested_action AND a tab_recommendation
 *     for the same tab.
 *
 * The rule: if a suggested_action with the same `tab_index` already
 * exists, drop the synthesised action so the tab card alone carries
 * the close button.
 */

/**
 * P0 §3.6: normalise the wire-format ``micro_steps`` payload into the
 * popup's controlled-list shape. The daemon currently emits the dict
 * form on every INTERVENTION_TRIGGER (a backwards-compat coercer
 * promotes any LLM-emitted strings to ``{text, status: "pending"}``);
 * however older daemons or future test fixtures may still ship the
 * raw ``string[]`` shape, so we accept both.
 */
export function normaliseMicroSteps(
    raw: unknown,
): Array<{ text: string; status: "pending" | "done" | "skipped" }> {
    if (!Array.isArray(raw)) return [];
    const out: Array<{ text: string; status: "pending" | "done" | "skipped" }> = [];
    for (const entry of raw) {
        if (typeof entry === "string") {
            if (entry.length > 0) out.push({ text: entry, status: "pending" });
            continue;
        }
        if (entry && typeof entry === "object") {
            const e = entry as Record<string, unknown>;
            const text = typeof e.text === "string" ? e.text : "";
            const rawStatus = typeof e.status === "string" ? e.status : "pending";
            const status: "pending" | "done" | "skipped" =
                rawStatus === "done" || rawStatus === "skipped" ? rawStatus : "pending";
            if (text.length > 0) out.push({ text, status });
        }
    }
    return out;
}

export function synthesizeActions(
    actions: Record<string, unknown>[],
    tabRecs: TabRecommendations | null,
): Record<string, unknown>[] {
    if (!tabRecs || !tabRecs.tabs || tabRecs.tabs.length === 0) return actions;
    const closeable = tabRecs.tabs.filter(
        (t: TabRecommendation) =>
            t.action === "close" || t.action === "bookmark_and_close"
    );
    if (closeable.length === 0) return actions;

    // Collect tab_index values already represented by an existing
    // close-style suggested_action.
    const coveredIndices = new Set<number>();
    for (const a of actions) {
        const at = a.action_type;
        if (at !== "close_tab" && at !== "bookmark_and_close") continue;
        const ti = typeof a.tab_index === "number" ? a.tab_index : Number(a.tab_index);
        if (Number.isFinite(ti)) coveredIndices.add(ti);
    }

    const synthesised: Record<string, unknown>[] = [];
    for (let i = 0; i < closeable.length; i++) {
        const t = closeable[i];
        const ti = typeof t.tab_index === "number" ? t.tab_index : Number(t.tab_index);
        if (!Number.isFinite(ti)) continue;
        if (coveredIndices.has(ti)) continue; // dedup: card already has close
        // Narrow the inferred action_type to the generated literal union
        // so a future rename in the Pydantic catalog surfaces here at
        // compile time (Debt-1).
        const action_type: SuggestedAction["action_type"] =
            t.action === "bookmark_and_close" ? "bookmark_and_close" : "close_tab";
        synthesised.push({
            action_id: `synth_${Date.now()}_${i}`,
            action_type,
            tab_index: ti,
            target: "",
            label: `Close ${t.tab_title || "tab"}`,
            reason: t.reason || "",
            category: "recommended" as SuggestedAction["category"],
            reversible: true,
            metadata: {},
        });
    }
    if (synthesised.length === 0) return actions;
    return [...actions, ...synthesised];
}

// --- State dot animation helper ---

function getStateDotStyle(stateStr: string, stateColor: string): React.CSSProperties {
    const base: React.CSSProperties = {
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: stateColor,
        flexShrink: 0,
    };

    switch (stateStr) {
        case "FLOW":
            return { ...base, animation: "cxPulse 2s ease-in-out infinite" };
        case "HYPO":
            return { ...base, animation: "cxFadeSlow 4s ease-in-out infinite" };
        case "HYPER":
            // No animation, no glow — student is already overwhelmed
            return base;
        default:
            return base;
    }
}

/**
 * F54: four distinct connectivity states for the popup connection
 * indicator + diagnostic block.
 *
 * - not_installed:           native messaging host missing
 * - installed_no_daemon:     native host present but daemon WS unreachable
 * - installed_version_mismatch: daemon up but its version disagrees with ours
 * - handshake_failed:        WS opened but daemon rejected handshake
 *
 * The connected boolean already covers the happy path; this enum
 * disambiguates the failure modes so each can carry its own
 * diagnostic and fix-action button. `ok` is the happy path.
 */
export type ConnectivityState =
    | "ok"
    | "not_installed"
    | "installed_no_daemon"
    | "installed_version_mismatch"
    | "handshake_failed";

export function classifyConnectivity(input: {
    connected: boolean;
    nativeHostStatus: "present" | "missing" | "unknown";
    daemonVersion: string | null;
    expectedVersion: string;
    handshakeError: string | null;
}): ConnectivityState {
    if (input.connected && input.handshakeError) return "handshake_failed";
    if (input.connected) {
        if (
            input.daemonVersion &&
            input.expectedVersion &&
            input.daemonVersion !== input.expectedVersion
        ) {
            return "installed_version_mismatch";
        }
        return "ok";
    }
    if (input.nativeHostStatus === "missing") return "not_installed";
    return "installed_no_daemon";
}

// --- "Last 7 days" sparkbar strip (P0 §3.2) ---

/**
 * Format a YYYY-MM-DD ``record_date`` as a 3-letter weekday for the
 * hover tooltip. Parsing it as ``Date(`${ymd}T00:00:00`)`` keeps the
 * weekday stable regardless of the host's UTC offset (a bare
 * ``new Date(ymd)`` shifts a day backward for users west of UTC).
 */
function weekdayShort(recordDate: string): string {
    try {
        const d = new Date(`${recordDate}T00:00:00`);
        if (Number.isNaN(d.getTime())) return recordDate;
        return d.toLocaleDateString(undefined, { weekday: "short" });
    } catch {
        return recordDate;
    }
}

/**
 * P0 §3.2: compact 7-sparkbar mini-row that sits between the Today
 * footer and the View history link. Each bar = ``DailyBaseline``;
 * height is proportional to ``total_flow_minutes`` over the week's
 * max; top-25-percentile bars take the terracotta accent so the
 * "best day" reads at a glance.
 *
 * Mount-time data flow:
 *   1. Ask background for the cached payload (``GET_CACHED_TRENDS``).
 *   2. Nudge a fresh fetch (``REQUEST_TRENDS``) — this also
 *      synchronously echoes back the cached payload so two race-paths
 *      converge on the same source of truth.
 *   3. Subscribe to ``TRENDS_READY`` broadcasts so a fresh WS frame
 *      that lands while the popup is open updates the bars live.
 */
function TrendsMiniStrip(): React.ReactElement {
    const [trends, setTrends] = useState<TrendsResponse | null>(null);
    // P0 §3.2 hardening: render a dedicated "temporarily unavailable"
    // copy when the background script's chrome.runtime.sendMessage
    // callback throws (port disconnected, SW evicted mid-flight, etc.)
    // rather than the generic empty state which implies "no data yet".
    // Reset to ``false`` whenever a subsequent call resolves so a
    // transient failure doesn't sticky the error UI.
    const [loadFailed, setLoadFailed] = useState(false);

    useEffect(() => {
        // 1) hydrate from cache
        try {
            chrome.runtime.sendMessage(
                { type: "GET_CACHED_TRENDS" },
                (raw: unknown) => {
                    // ``chrome.runtime.lastError`` populates inside the
                    // callback when the background SW disconnected mid-
                    // call; treat that the same as a thrown error.
                    // F18 (Phase-4 audit): centralised lastError reader.
                    const lastErr = getLastRuntimeError();
                    if (lastErr) {
                        if (CORTEX_DEBUG) {
                            console.warn(
                                "[cortex.popup] GET_CACHED_TRENDS lastError",
                                lastErr.message,
                            );
                        }
                        setLoadFailed(true);
                        return;
                    }
                    // Successful response — clear any prior failure
                    // state so the next render swaps the error copy
                    // back out for bars or the empty state.
                    setLoadFailed(false);
                    const resp = raw as
                        | { trends: TrendsResponse | null; timestamp: number | null }
                        | undefined;
                    if (resp?.trends) {
                        setTrends(resp.trends);
                    }
                    // 2) nudge a refresh if the cached payload is stale
                    // (or absent). The background script echoes back
                    // whatever it already has, so this also acts as a
                    // second hydration path for popups opened before
                    // the first GET_CACHED_TRENDS callback resolves.
                    //
                    // Phase 4 hardening: use a nullable timestamp
                    // rather than collapsing missing-cache to epoch 0.
                    // ``ts === 0`` previously short-circuited the
                    // staleness check on a 1970-vintage cache (which
                    // can never occur on a real wall-clock) but also
                    // hid the "no cache at all" case behind the same
                    // branch. Splitting them keeps the staleness math
                    // purely about wall-clock age.
                    const ts: number | null = resp?.timestamp ?? null;
                    const stale =
                        !resp?.trends ||
                        ts === null ||
                        Date.now() - ts > TRENDS_STALENESS_MS;
                    if (stale) {
                        try {
                            chrome.runtime.sendMessage(
                                { type: "REQUEST_TRENDS" },
                                (raw2: unknown) => {
                                    // F18: centralised lastError reader.
                                    const lastErr2 = getLastRuntimeError();
                                    if (lastErr2) {
                                        if (CORTEX_DEBUG) {
                                            console.warn(
                                                "[cortex.popup] REQUEST_TRENDS lastError",
                                                lastErr2.message,
                                            );
                                        }
                                        setLoadFailed(true);
                                        return;
                                    }
                                    setLoadFailed(false);
                                    const resp2 = raw2 as
                                        | { trends: TrendsResponse | null; timestamp: number | null }
                                        | undefined;
                                    if (resp2?.trends) setTrends(resp2.trends);
                                },
                            );
                        } catch (err) {
                            // sendMessage may throw in odd contexts —
                            // surface as the error UI rather than the
                            // (misleading) empty-state copy.
                            if (CORTEX_DEBUG) {
                                console.warn(
                                    "[cortex.popup] REQUEST_TRENDS threw",
                                    err,
                                );
                            }
                            setLoadFailed(true);
                        }
                    }
                },
            );
        } catch (err) {
            // chrome.runtime unavailable; render the error UI rather
            // than the (misleading) empty-state copy.
            if (CORTEX_DEBUG) {
                console.warn(
                    "[cortex.popup] GET_CACHED_TRENDS threw",
                    err,
                );
            }
            setLoadFailed(true);
        }
    }, []);

    // 3) live updates from the background script.
    const trendsListener = useCallback((msg: Record<string, unknown>) => {
        if (msg.type !== "TRENDS_READY") return;
        const payload = msg.payload as TrendsResponse | undefined;
        if (payload) {
            // A live update is implicit proof that the runtime port is
            // healthy — clear any lingering loadFailed flag so the
            // strip swaps the error copy for fresh bars.
            setLoadFailed(false);
            setTrends(payload);
        }
    }, []);

    useEffect(() => {
        chrome.runtime.onMessage.addListener(trendsListener);
        return () => chrome.runtime.onMessage.removeListener(trendsListener);
    }, [trendsListener]);

    // P0 §3.2 hardening: if the background script could not be reached
    // and we have nothing cached locally to fall back on, show the
    // error copy in place of the (misleading) empty-state guidance.
    // If we DO have cached data, prefer to render it over the error
    // copy — stale bars are more useful than no bars.
    if (loadFailed && !trends) {
        return (
            <div style={S.trendsStrip} data-testid="trends-strip">
                <div style={S.trendsHeader}>
                    <span style={S.trendsTitle}>Last 7 days</span>
                </div>
                <div
                    style={S.trendsEmpty}
                    data-testid="trends-error"
                    role="status"
                    aria-live="polite"
                >
                    Trends temporarily unavailable
                </div>
            </div>
        );
    }

    // Slice to the trailing 7 days so a daemon that returns more than
    // a week (e.g. ``window=month`` snuck in by a bug) still renders
    // exactly 7 bars without overflowing the strip.
    const daily: DailyBaseline[] = (trends?.daily ?? []).slice(-7);
    const minutes = daily.map((d) => Math.max(0, Math.round(d.total_flow_minutes ?? 0)));
    const maxMin = minutes.reduce((m, v) => (v > m ? v : m), 0);
    const totalMin = minutes.reduce((s, v) => s + v, 0);
    const avgMin = daily.length > 0 ? Math.round(totalMin / daily.length) : 0;

    // Empty state: render guidance copy when we have no rows or every
    // row is zero minutes. The tertiary label colour keeps it from
    // competing with the Today footer numbers above.
    const isEmpty = daily.length === 0 || maxMin === 0;
    if (isEmpty) {
        return (
            <div style={S.trendsStrip} data-testid="trends-strip">
                <div style={S.trendsHeader}>
                    <span style={S.trendsTitle}>Last 7 days</span>
                </div>
                <div style={S.trendsEmpty} data-testid="trends-empty">
                    Not enough data yet. Run a few sessions.
                </div>
            </div>
        );
    }

    // Top-quartile threshold: if we have fewer than 4 days every bar
    // is hot (the "quartile" concept is meaningless with <4 samples
    // and demoting them all to grey would hide the only signal we
    // have). Otherwise compute the 75th percentile on the sorted
    // ascending minutes list and compare strictly greater than.
    let hotThreshold = -1;
    if (daily.length >= 4) {
        const sorted = [...minutes].sort((a, b) => a - b);
        const idx = Math.floor(sorted.length * 0.75);
        hotThreshold = sorted[Math.min(idx, sorted.length - 1)];
    }

    return (
        <div style={S.trendsStrip} data-testid="trends-strip">
            <div style={S.trendsHeader}>
                <span style={S.trendsTitle}>Last 7 days</span>
                {/* P2-4: show a "Stale" badge when we have cached trends but
                    the latest refresh attempt failed. The chart stays visible
                    so the user still sees their data; the badge signals it
                    may be outdated. */}
                {loadFailed && (
                    <span
                        data-testid="trends-stale-badge"
                        style={{
                            fontSize: 9,
                            color: CX.textTertiary,
                            background: CX.surface,
                            border: `1px solid ${CX.borderDefault}`,
                            borderRadius: 4,
                            padding: "1px 5px",
                            fontFamily: CX.mono,
                            textTransform: "uppercase" as const,
                            letterSpacing: "0.04em",
                            alignSelf: "center",
                        }}
                    >
                        Stale
                    </span>
                )}
                <span style={S.trendsAvg} data-testid="trends-avg">
                    {avgMin} min avg/day
                </span>
            </div>
            <div style={S.trendsBars} role="img" aria-label={`Last ${daily.length} days of focus minutes per day`}>
                {daily.map((d, i) => {
                    const v = minutes[i];
                    const isHot = daily.length < 4 ? true : v > hotThreshold;
                    // Bars always render at least 2px tall when v>0 so
                    // a non-zero-but-tiny day is still visible; 0-min
                    // days render at 2px in the tertiary colour as a
                    // "we tried" marker so the strip's gap doesn't
                    // imply missing data.
                    const heightPx =
                        v === 0
                            ? 2
                            : Math.max(2, Math.round((v / maxMin) * 16));
                    const color = isHot && v > 0 ? CX.accent : CX.textTertiary;
                    return (
                        <div
                            key={d.record_date ?? `d${i}`}
                            data-testid={`trends-bar-${i}`}
                            data-hot={isHot && v > 0 ? "true" : "false"}
                            title={`${weekdayShort(d.record_date ?? "")}: ${v} min`}
                            style={{
                                width: 6,
                                height: heightPx,
                                background: color,
                                borderRadius: 1,
                                alignSelf: "flex-end",
                            }}
                        />
                    );
                })}
            </div>
        </div>
    );
}

// --- Main ---

function CortexPopup(): React.ReactElement {
    const [connected, setConnected] = useState(false);
    const [nativeHostStatus, setNativeHostStatus] = useState<"present" | "missing" | "unknown">("unknown");
    const [daemonVersion, setDaemonVersion] = useState<string | null>(null);
    const [handshakeError, setHandshakeError] = useState<string | null>(null);
    // F21 (Phase-4 audit / §3.15): BYOK cost indicator — hidden when
    // the daemon reports provider="none" / "rule_based" (no LLM
    // spend), shown as a one-line "$0.07 / $5.00 today" pill otherwise.
    const [costInfo, setCostInfo] = useState<{
        cost_today: number;
        budget_today: number;
        provider: string | null;
        budget_exhausted: boolean;
    } | null>(null);
    const [state, setState] = useState<CortexState | null>(null);
    const [focus, setFocus] = useState<FocusSnapshot | null>(null);
    const [dailyStats, setDailyStats] = useState<DailyStats | null>(null);
    const [goalInput, setGoalInput] = useState("");
    const [alert, setAlert] = useState<{ title: string; body: string } | null>(null);
    const [activeActions, setActiveActions] = useState<Record<string, unknown>[]>([]);
    const [tabRecs, setTabRecs] = useState<TabRecommendations | null>(null);
    const [errAnalysis, setErrAnalysis] = useState<Record<string, string> | null>(null);
    const [interventionId, setInterventionId] = useState<string>("");
    const [applied, setApplied] = useState(false);
    // P1-FC-INTERVENTION-FAILED: when the daemon reports that an
    // intervention's mutations ALL failed (workspace NOT changed), the
    // card flips to an error state and the apply CTA is disabled. Holds
    // the user-facing reason string, or null when not failed.
    const [interventionError, setInterventionError] = useState<string | null>(
        null,
    );
    // P1-FC-INTERVENTION-PROMPT: cross-surface micro-commit / movement-
    // break prompt text rendered informationally above the action card.
    const [interventionPrompt, setInterventionPrompt] = useState<string | null>(
        null,
    );
    const [causalExplanation, setCausalExplanation] = useState<string>("");
    // P0 §3.6: micro-step checklist. The wire payload may carry either
    // the legacy ``string[]`` shape or the new ``{text, status, …}[]``
    // shape; we normalise on ingest so the render stays simple. The
    // status round-trips to the daemon via MICRO_STEP_TOGGLED so a tick
    // here mutates the active plan and rebroadcasts strikethrough
    // styling to every connected surface.
    const [microSteps, setMicroSteps] = useState<
        Array<{ text: string; status: "pending" | "done" | "skipped" }>
    >([]);
    const [briefing, setBriefing] = useState<MorningBriefing | null>(null);
    const [tabCloseDisabled, setTabCloseDisabled] = useState(false);
    const [quietMode, setQuietMode] = useState(false);
    // P0 §3.11: surface the active quiet/pause kind + countdown so the
    // popup pill says "Snoozed · 12m" instead of just "Quiet".
    const [quietModeKind, setQuietModeKind] = useState<string>("off");
    const [quietModeEndsAt, setQuietModeEndsAt] = useState<number | null>(null);
    // Phase-3 P0-3 + Audit-1.2 F4: derive remaining minutes live from
    // ``quietModeEndsAt`` so the countdown ticks down without waiting
    // on a daemon re-broadcast. ``quietModeDurationMin`` is now a
    // ``useState`` only for the initial-paint value; the useEffect
    // below replaces it with a tick-on-every-second update.
    const [quietModeDurationMin, setQuietModeDurationMin] = useState<number | null>(null);
    useEffect(() => {
        // F8 (Phase-4 audit): include ``quietMode`` in the dep array so
        // the interval restarts (and the tick re-evaluates) when the
        // user toggles quiet mode while a countdown is armed. Without
        // ``quietMode`` here, an off-toggle leaked a running interval
        // that kept overwriting ``quietModeDurationMin`` with stale
        // values for the rest of the popup's lifetime.
        if (quietModeEndsAt === null || !quietMode) {
            setQuietModeDurationMin(null);
            return;
        }
        const tick = () => {
            const remainingMs = quietModeEndsAt - Date.now();
            if (remainingMs <= 0) {
                setQuietModeDurationMin(0);
                return;
            }
            setQuietModeDurationMin(Math.max(0, Math.round(remainingMs / 60000)));
        };
        tick();
        const handle = setInterval(tick, 30_000); // 30s is plenty for minute-grain countdown
        return () => clearInterval(handle);
    }, [quietModeEndsAt, quietMode]);
    const [launching, setLaunching] = useState(false);
    const [launchError, setLaunchError] = useState(false);
    const [tabsExpanded, setTabsExpanded] = useState(false);
    // P0 §3.8: rating state. ``rating`` is the user's current 👍/👎
    // selection on the active intervention; null when not rated.
    // ``ratingTextOpen`` toggles the inline one-line input on 👎.
    const [rating, setRating] = useState<"thumbs_up" | "thumbs_down" | null>(null);
    const [ratingTextOpen, setRatingTextOpen] = useState<boolean>(false);
    const [ratingText, setRatingText] = useState<string>("");
    // P0 §3.8 audit fix: track the current intervention level so the
    // rating row only renders on guided_mode + simplified_workspace
    // overlays (minimal-tone overlay_only interventions should stay
    // ambient per spec line 710).
    const [interventionLevel, setInterventionLevel] = useState<
        "overlay_only" | "simplified_workspace" | "guided_mode"
    >("overlay_only");
    // P0 §3.9: structured causal signals + the "Why?" expander state.
    // ``causalSignals`` is the array pushed by INTERVENTION_TRIGGER or
    // fetched on demand via WHY_DETAIL_REQUEST.
    const [causalSignals, setCausalSignals] = useState<
        { name: string; current_value: number; baseline_value: number | null;
          unit: string; delta_pct: number | null; samples_60s: number[];
          severity: "primary" | "secondary" | "tertiary"; }[]
    >([]);
    const [whyOpen, setWhyOpen] = useState<boolean>(false);
    // P0 §3.9 audit fix: surface the WhyDetail.error field so the popup
    // can explain *why* the drilldown is empty instead of just rendering
    // a blank panel when the daemon returns ``error="not_found"`` or
    // similar. Reset on each new INTERVENTION_TRIGGER.
    const [whyError, setWhyError] = useState<string | null>(null);
    // P0 §3.7: BREAK_RECOMMENDATION pulse from the daemon. When set we
    // render a soft pill above the intervention card with a one-click
    // "Take a 4-minute break" CTA.
    const [breakRec, setBreakRec] = useState<{
        reason: string;
        urgency: "low" | "medium" | "high";
        stress_load: number;
        threshold: number;
        duration_seconds: number;
        breathing_pattern: "box" | "4-7-8" | "coherent";
    } | null>(null);
    // P0 §3.3: end-of-session recap card. ``recap`` is the cached
    // SessionReport; ``recapTimestamp`` is when the background script
    // wrote it. ``historyStatus`` carries the response from
    // OPEN_DASHBOARD_HISTORY when the native host is unavailable so we
    // can render a one-line install hint.
    const [recap, setRecap] = useState<SessionReport | null>(null);
    const [recapTimestamp, setRecapTimestamp] = useState<number | null>(null);
    // C4: ``persisted`` from the SessionRecap wrapper — false when the
    // broadcast raced ahead of the atomic disk write, so the card can
    // signal that history may lag the live recap.
    const [recapPersisted, setRecapPersisted] = useState<boolean | null>(null);
    const [historyStatus, setHistoryStatus] = useState<string>("");
    // Phase 4d Task H / §3.24: in-app bug report.
    const [bugReportOpen, setBugReportOpen] = useState(false);
    const [bugReportText, setBugReportText] = useState("");
    const [bugReportIncludeLogs, setBugReportIncludeLogs] = useState(true);
    const [bugReportStatus, setBugReportStatus] = useState<
        "idle" | "submitting" | "saved" | "queued" | "error"
    >("idle");
    const [bugReportError, setBugReportError] = useState<string>("");

    // F21 (Phase-4 audit / §3.15 follow-up): poll the daemon's
    // /api/cost endpoint every 30s while the popup is open. The
    // background script proxies the fetch so we don't need
    // cross-origin credentials. Stops polling if the popup closes
    // (component unmount).
    useEffect(() => {
        if (!connected) return;
        let cancelled = false;
        const fetchCost = () => {
            safeSendMessage({ type: "GET_COST" }, (raw: unknown) => {
                if (cancelled) return;
                const resp = raw as
                    | {
                          ok?: boolean;
                          cost?: {
                              cost_today: number;
                              budget_today: number;
                              provider?: string | null;
                              budget_exhausted?: boolean;
                              prompt_tokens?: number | null;
                              completion_tokens?: number | null;
                              model?: string | null;
                              timestamp?: number;
                          };
                      }
                    | undefined;
                if (resp?.ok && resp.cost) {
                    setCostInfo({
                        cost_today: resp.cost.cost_today,
                        budget_today: resp.cost.budget_today,
                        provider: resp.cost.provider ?? null,
                        budget_exhausted: resp.cost.budget_exhausted === true,
                    });
                }
            });
        };
        fetchCost();
        const handle = setInterval(fetchCost, 30_000);
        return () => {
            cancelled = true;
            clearInterval(handle);
        };
    }, [connected]);

    // Inject fonts + keyframes (single injection point)
    useEffect(() => {
        const id = "cortex-popup-styles";
        if (document.getElementById(id)) return;
        const style = document.createElement("style");
        style.id = id;
        style.textContent = CX_KEYFRAMES + `
            @keyframes cxAlertIn {
                from { transform: translateY(-8px); opacity: 0; }
                to { transform: translateY(0); opacity: 1; }
            }
            .cortex-goal-input:focus-visible {
                outline: 2px solid ${CX.accent};
                outline-offset: 2px;
            }
        `;
        document.head.appendChild(style);
        return () => { style.remove(); };
    }, []);

    // Load tab-close and quiet-mode toggle states on mount
    useEffect(() => {
        chrome.storage.local.get("cortex_tab_close_disabled", (result) => {
            if (result.cortex_tab_close_disabled === true) {
                setTabCloseDisabled(true);
            }
        });
        // Phase-3 P0-2 + Audit-1.2 F2: rehydrate the full quiet-mode
        // envelope on mount so the pill doesn't lie before the daemon's
        // first QUIET_MODE_STATE broadcast lands. Background.ts
        // persists ``cortex_quiet_state`` on every QUIET_MODE_STATE
        // it forwards to the popup.
        chrome.storage.session.get(
            ["quietMode", "cortex_quiet_state"],
            (result) => {
                if (result.quietMode === true) setQuietMode(true);
                const cached = result.cortex_quiet_state as
                    | Record<string, unknown>
                    | undefined;
                if (cached && typeof cached.kind === "string") {
                    const kind = cached.kind as string;
                    setQuietModeKind(kind);
                    setQuietMode(kind !== "off");
                    if (typeof cached.ends_at === "number") {
                        setQuietModeEndsAt(cached.ends_at as number);
                    }
                    if (typeof cached.duration_minutes === "number") {
                        setQuietModeDurationMin(cached.duration_minutes as number);
                    }
                }
            },
        );
    }, []);

    const handleTabCloseToggle = useCallback(() => {
        const newValue = !tabCloseDisabled;
        setTabCloseDisabled(newValue);
        chrome.storage.local.set({ cortex_tab_close_disabled: newValue });
    }, [tabCloseDisabled]);

    const handleQuietModeToggle = useCallback(() => {
        const newValue = !quietMode;
        setQuietMode(newValue);
        // Legacy wire — kept for back-compat with older daemons that
        // only know the boolean toggle. New daemons mirror SETTINGS_SYNC
        // from the canonical QUIET_MODE_TOGGLE path below.
        sendWithCid({ type: "TOGGLE_QUIET_MODE", quiet: newValue });
        // P0 §3.11: also emit the canonical QUIET_MODE_TOGGLE so the
        // daemon broadcasts the unified QUIET_MODE_STATE back.
        sendWithCid({
            type: "QUIET_MODE_TOGGLE",
            kind: newValue ? "quiet_session" : "off",
            duration_minutes: null,
        });
    }, [quietMode]);

    const handleQuietModeKind = useCallback(
        (kind: "snooze_15" | "quiet_session" | "pause" | "off") => {
            // Optimistic local update — the daemon's QUIET_MODE_STATE
            // broadcast will reconcile within a few hundred ms.
            setQuietModeKind(kind);
            setQuietMode(kind !== "off");
            sendWithCid({
                type: "QUIET_MODE_TOGGLE",
                kind,
                duration_minutes: kind === "snooze_15" ? 15 : null,
            });
        },
        [],
    );

    const [launchStatus, setLaunchStatus] = useState("");

    // F54: self-derived from the extension manifest so a future
    // ``package.json`` version bump cannot desync the check against
    // itself. Tests can still override via the ``classifyConnectivity``
    // helper which accepts ``expectedVersion`` as an explicit arg.
    const EXPECTED_VERSION: string = (() => {
        try {
            const manifest = chrome?.runtime?.getManifest?.();
            const v = manifest?.version;
            if (typeof v === "string" && v.length > 0) return v;
        } catch { /* chrome.runtime not available in some test envs */ }
        return "0.0.0";
    })();
    const connectivity = classifyConnectivity({
        connected,
        nativeHostStatus,
        daemonVersion,
        expectedVersion: EXPECTED_VERSION,
        handshakeError,
    });

    const handleLaunchCortex = useCallback(() => {
        setLaunching(true);
        setLaunchError(false);
        setLaunchStatus("Launching daemon\u2026");
        sendWithCid({ type: "LAUNCH_CORTEX" }, (raw: unknown) => {
            const resp = raw as { ok?: boolean; status?: string; error?: string } | undefined;
            if (resp?.ok && resp.status === "camera_enabled") {
                setLaunching(false);
                setLaunchStatus("");
            } else {
                setLaunching(false);
                setLaunchError(true);
                const errorMsg = resp?.error || "Could not reach daemon";
                setLaunchStatus(`Start failed: ${errorMsg}. Run in terminal: python -m cortex.scripts.run_dev`);
                setTimeout(() => { setLaunchError(false); setLaunchStatus(""); }, 30000);
            }
        });
    }, []);

    useEffect(() => {
        safeSendMessage({ type: "GET_STATE" }, (raw) => {
            const resp = raw as {
                connected: boolean;
                state: CortexState | null;
                focusSession: FocusSnapshot | null;
                intervention?: Record<string, unknown>;
            } | undefined;
            if (!resp) return;
            setConnected(resp.connected);
            setState(resp.state);
            setFocus(resp.focusSession);
            if (resp.intervention) {
                const p = resp.intervention;
                const rawActions = (p.suggested_actions as Record<string, unknown>[]) || [];
                const recs = (p.tab_recommendations as TabRecommendations | undefined) ?? null;
                setActiveActions(synthesizeActions(rawActions, recs));
                setTabRecs(recs);
                setErrAnalysis((p.error_analysis as Record<string, string>) || null);
                setInterventionId(String(p.intervention_id || ""));
                setMicroSteps(normaliseMicroSteps(p.micro_steps));
                setApplied(false);
            }
        });
        safeSendMessage({ type: "GET_DAILY_STATS" }, (raw) => {
            const stats = raw as DailyStats | undefined;
            if (stats) setDailyStats(stats);
        });
        // G2 (audit-prod): ask the background script to re-run its
        // connectivity probe so the popup renders a fresh diagnostic
        // (native-host / daemon-version / handshake) at open time.
        safeSendMessage({ type: "REQUEST_CONNECTIVITY_DIAGNOSTIC" });
        // P0 §3.3: pull the cached recap so we can render the card
        // immediately. Only adopt it if it's still inside the 24h TTL.
        safeSendMessage({ type: "GET_CACHED_RECAP" }, (raw) => {
            // C4: the cached recap is the SessionRecap wrapper
            // ``{report, generated_at, persisted}``; the renderable
            // SessionReport is at ``recap.report``.
            const resp = raw as
                | { recap: SessionRecap | null; timestamp: number | null }
                | undefined;
            const report = resp?.recap?.report ?? null;
            if (!report) return;
            const ts = resp?.timestamp ?? 0;
            if (ts > 0 && Date.now() - ts > RECAP_TTL_MS) return;
            setRecap(report as SessionReport);
            setRecapPersisted(resp?.recap?.persisted ?? null);
            setRecapTimestamp(ts);
            // Card is now visible to the user — clear the toolbar badge.
            safeSendMessage({ type: "RECAP_VIEWED" });
        });
    }, []);

    // F50: stable listener identity so addListener/removeListener
    // refer to the same function across re-renders. Pinning with
    // ``useCallback([])`` ensures the cleanup function in the effect
    // below sees the exact same reference. Phase G (Debt-1) tightened
    // the ``TabRecommendations`` cast on INTERVENTION_TRIGGER so the
    // generated schema enforces the shape at compile time.
    const popupMessageListener = useCallback((msg: Record<string, unknown>) => {
        switch (msg.type) {
            case "CONNECTION_CHANGED":
                setConnected(msg.connected as boolean);
                break;
            case "STATE_UPDATE":
                setState(msg.payload as CortexState);
                if (msg.focusSession) setFocus(msg.focusSession as FocusSnapshot);
                break;
            case "FOCUS_SESSION_STARTED": {
                // Phase-3 / Audit-1.2 F3: spec calls for a distinct
                // popup pill when the daemon (not the user) armed the
                // focus session. Background broadcasts ``autoArmed``,
                // ``preset``, ``endsAt`` — adopt them into local state
                // so the FocusPill component can branch on the kind.
                const goal = typeof msg.goal === "string"
                    ? (msg.goal as string)
                    : "Focus session";
                const autoArmed = msg.autoArmed === true;
                const preset = typeof msg.preset === "string"
                    ? (msg.preset as string)
                    : undefined;
                const endsAt = typeof msg.endsAt === "number"
                    ? (msg.endsAt as number)
                    : null;
                setFocus({
                    elapsedMs: 0,
                    focusMs: 0,
                    focusPct: 0,
                    distractionsBlocked: 0,
                    longestStreakMin: 0,
                    currentStreakMs: 0,
                    goal,
                    autoArmed,
                    preset,
                    endsAt,
                });
                break;
            }
            case "FOCUS_SESSION_ENDED":
                setFocus(null);
                safeSendMessage({ type: "GET_DAILY_STATS" }, (raw) => {
                    const stats = raw as DailyStats | undefined;
                    if (stats) setDailyStats(stats);
                });
                break;
            case "HEALTH_ALERT":
                setAlert({ title: msg.title as string, body: msg.body as string });
                setTimeout(() => setAlert(null), 10000);
                break;
            case "BREAK_SUGGESTED":
                setAlert({ title: "Time for a break", body: msg.reason as string });
                setTimeout(() => setAlert(null), 10000);
                break;
            case "INTERVENTION_TRIGGER": {
                const p = msg.payload as Record<string, unknown>;
                const rawActions = (p.suggested_actions as Record<string, unknown>[]) || [];
                const recs = (p.tab_recommendations as TabRecommendations | undefined) ?? null;
                setActiveActions(synthesizeActions(rawActions, recs));
                setTabRecs(recs);
                setErrAnalysis((p.error_analysis as Record<string, string>) || null);
                setInterventionId(String(p.intervention_id || ""));
                setCausalExplanation(String(p.causal_explanation || ""));
                // P0 §3.8 audit fix (spec line 710): only show the
                // rating row on guided_mode + simplified_workspace
                // overlays — minimal-tone overlay_only interventions
                // should stay ambient and not solicit ratings.
                const level = String(p.level || "");
                setInterventionLevel(
                    level === "guided_mode" || level === "simplified_workspace"
                        ? level
                        : "overlay_only",
                );
                // P0 §3.6: ingest the new ``micro_steps`` shape so the
                // popup's controlled checklist re-renders strikethrough
                // styling when another surface (overlay / VS Code panel)
                // ticks a step.
                setMicroSteps(normaliseMicroSteps(p.micro_steps));
                // P0 §3.9: adopt structured causal signals from the
                // intervention trigger payload (top 2-3 ranked drivers).
                // The wire shape is validated by the daemon's Pydantic
                // model; we narrow to ``CausalSignalSchema[]`` after the
                // runtime guard so a future schema-rename surfaces here
                // as a TS error.
                const signals = (p.causal_signals as unknown[]) || [];
                // Each raw entry is validated against ``CausalSignalSchema``
                // by the daemon; we narrow at the wire boundary so a
                // future schema-rename surfaces as a TS error rather
                // than a silent ``Number(undefined) === NaN``.
                setCausalSignals(
                    signals
                        .filter((s): s is Record<string, unknown> => typeof s === "object" && s !== null)
                        .map((s) => {
                            const typed = s as Partial<CausalSignalSchema>;
                            return {
                                name: String(typed.name ?? ""),
                                current_value: Number(typed.current_value ?? 0),
                                baseline_value: typed.baseline_value == null ? null : Number(typed.baseline_value),
                                unit: String(typed.unit ?? ""),
                                delta_pct: typed.delta_pct == null ? null : Number(typed.delta_pct),
                                samples_60s: Array.isArray(typed.samples_60s)
                                    ? typed.samples_60s.map((v) => Number(v))
                                    : [],
                                severity:
                                    typed.severity === "primary" || typed.severity === "tertiary"
                                        ? typed.severity
                                        : ("secondary" as const),
                            };
                        }),
                );
                // P0 §3.8: reset rating state when a new intervention arrives.
                setRating(null);
                setRatingTextOpen(false);
                setRatingText("");
                setWhyOpen(false);
                setWhyError(null);
                setApplied(false);
                // A fresh intervention clears any prior failure/prompt
                // state so the card starts clean.
                setInterventionError(null);
                setInterventionPrompt(null);
                break;
            }
            case "INTERVENTION_RESTORE":
                setActiveActions([]);
                setTabRecs(null);
                setErrAnalysis(null);
                setCausalExplanation("");
                setMicroSteps([]);
                setCausalSignals([]);
                setRating(null);
                setRatingTextOpen(false);
                setRatingText("");
                setWhyOpen(false);
                setWhyError(null);
                setApplied(false);
                // P0 §3.7 audit fix: when the underlying intervention
                // ends (dismiss / engage / restore), the standalone
                // BREAK_RECOMMENDATION pill must clear too. Without
                // this, a stale pill from the prior intervention
                // remained on screen and clicking its CTA dispatched
                // EXECUTE_ACTION with a dangling intervention_id.
                setBreakRec(null);
                setInterventionError(null);
                setInterventionPrompt(null);
                break;
            case "INTERVENTION_FAILED": {
                // P1-FC-INTERVENTION-FAILED: the daemon's executor returned
                // only failed mutations — the workspace was NOT changed.
                // Flip the card to an error state and disable the apply
                // CTA so the user isn't told to engage with a broken plan.
                const p = msg.payload as Record<string, unknown>;
                const reason = String(p.error_reason || "").trim();
                const failed = Array.isArray(p.failed_action_types)
                    ? (p.failed_action_types as unknown[]).map((a) =>
                          String(a).replace(/_/g, " "),
                      )
                    : [];
                const body = reason
                    ? reason
                    : failed.length > 0
                      ? `Couldn't apply: ${failed.join(", ")}.`
                      : "Couldn't apply — check extension permissions";
                setInterventionError(body);
                // Treat the failed intervention as "applied" so the CTA is
                // disabled; the error banner explains why.
                setApplied(true);
                break;
            }
            case "INTERVENTION_PROMPT": {
                // P1-FC-INTERVENTION-PROMPT: cross-surface prompt sync. Show
                // the prompt text inline above the action card so a
                // popup-open user has awareness of the active prompt.
                const p = msg.payload as Record<string, unknown>;
                const prompt = String(p.prompt || "").trim();
                setInterventionPrompt(prompt.length > 0 ? prompt : null);
                break;
            }
            case "BREAK_RECOMMENDATION": {
                // P0 §3.7: BREAK_RECOMMENDATION pulse relayed from
                // background.ts. Typed against the generated
                // ``BreakRecommendationSchema`` so a future schema-rename
                // produces a TS error instead of a silent fallthrough.
                const p = msg.payload as Partial<BreakRecommendationSchema> & Record<string, unknown>;
                setBreakRec({
                    reason: String(p.reason ?? "stress_integral_crossed_threshold"),
                    urgency:
                        p.urgency === "high" || p.urgency === "low"
                            ? (p.urgency as "low" | "high")
                            : "medium",
                    stress_load: Number(p.stress_load ?? 0),
                    threshold: Number(p.threshold ?? 0),
                    duration_seconds: Number(p.duration_seconds ?? 240),
                    breathing_pattern:
                        p.breathing_pattern === "4-7-8" ||
                        p.breathing_pattern === "coherent"
                            ? (p.breathing_pattern as "4-7-8" | "coherent")
                            : "box",
                });
                break;
            }
            case "WHY_DETAIL": {
                // P0 §3.9: on-demand reply to WHY_DETAIL_REQUEST. Typed
                // against the generated ``WhyDetailSchema`` so a future
                // rename of ``causal_signals``/``error`` surfaces here
                // as a TS error rather than a silently-empty drilldown.
                const p = msg.payload as Partial<WhyDetailSchema> & Record<string, unknown>;
                const sigs = (p.causal_signals as unknown[]) || [];
                setCausalSignals(
                    sigs
                        .filter((s): s is Record<string, unknown> => typeof s === "object" && s !== null)
                        .map((s) => {
                            const typed = s as Partial<CausalSignalSchema>;
                            return {
                                name: String(typed.name ?? ""),
                                current_value: Number(typed.current_value ?? 0),
                                baseline_value: typed.baseline_value == null ? null : Number(typed.baseline_value),
                                unit: String(typed.unit ?? ""),
                                delta_pct: typed.delta_pct == null ? null : Number(typed.delta_pct),
                                samples_60s: Array.isArray(typed.samples_60s)
                                    ? typed.samples_60s.map((v) => Number(v))
                                    : [],
                                severity:
                                    typed.severity === "primary" || typed.severity === "tertiary"
                                        ? typed.severity
                                        : ("secondary" as const),
                            };
                        }),
                );
                // Surface the error string from the daemon when it
                // could not attribute (e.g. ``handler_not_registered``,
                // ``not_found``) so the drilldown renders a one-line
                // dimmed cause instead of a misleading empty state.
                const errVal = (p as { error?: unknown }).error;
                setWhyError(typeof errVal === "string" && errVal.length > 0 ? errVal : null);
                setWhyOpen(true);
                break;
            }
            case "SETTINGS_SYNC": {
                const settings = msg.payload as Record<string, unknown>;
                if (typeof settings.quiet_mode === "boolean") {
                    setQuietMode(settings.quiet_mode);
                }
                break;
            }
            case "QUIET_MODE_STATE": {
                // P0 §3.11: daemon broadcast of the active quiet/pause
                // mode. Mirror onto local state so the toolbar pill
                // surfaces ("Snoozed · 12m") and the toggle reflects.
                const state = msg.payload as Record<string, unknown>;
                const kind = typeof state.kind === "string" ? state.kind : "off";
                setQuietModeKind(kind);
                setQuietMode(kind !== "off");
                setQuietModeEndsAt(
                    typeof state.ends_at === "number" ? (state.ends_at as number) : null,
                );
                setQuietModeDurationMin(
                    typeof state.duration_minutes === "number"
                        ? (state.duration_minutes as number)
                        : null,
                );
                break;
            }
            case "MORNING_BRIEFING": {
                const b = msg.payload as Record<string, unknown>;
                setBriefing({
                    summary: String(b.summary || ""),
                    action_items: (b.action_items as string[]) || [],
                    left_off_at: String(b.left_off_at || ""),
                });
                break;
            }
            case "CONNECTIVITY_DIAGNOSTIC": {
                // F54: background pushes the resolved diagnostic so the
                // popup can pick the right disconnected-state UI.
                const d = msg.payload as Record<string, unknown>;
                if (d.native_host_status === "present" || d.native_host_status === "missing") {
                    setNativeHostStatus(d.native_host_status as "present" | "missing");
                }
                if (typeof d.daemon_version === "string") setDaemonVersion(d.daemon_version);
                if (d.daemon_version === null) setDaemonVersion(null);
                if (typeof d.handshake_error === "string") setHandshakeError(d.handshake_error);
                if (d.handshake_error === null) setHandshakeError(null);
                break;
            }
            case "SESSION_RECAP_READY": {
                // P0 §3.3: background script just received a fresh recap
                // over WS. Adopt it immediately so a popup that was
                // already open re-renders without waiting for the next
                // mount, then clear the badge — the user is looking.
                //
                // C4: the payload is the SessionRecap wrapper
                // ``{report, generated_at, persisted}``; read the
                // renderable SessionReport from ``payload.report`` and the
                // durability flag from ``payload.persisted``.
                const wrapper = msg.payload as SessionRecap | undefined;
                const next = wrapper?.report ?? null;
                if (!next) break;
                const ts =
                    typeof msg.timestamp === "number"
                        ? (msg.timestamp as number)
                        : Date.now();
                setRecap(next as SessionReport);
                setRecapPersisted(wrapper?.persisted ?? null);
                setRecapTimestamp(ts);
                safeSendMessage({ type: "RECAP_VIEWED" });
                break;
            }
        }
    }, []);

    useEffect(() => {
        chrome.runtime.onMessage.addListener(popupMessageListener);
        return () => chrome.runtime.onMessage.removeListener(popupMessageListener);
    }, [popupMessageListener]);

    const handleConnect = useCallback(() => {
        sendWithCid({ type: "CONNECT" });
    }, []);

    // P0 §3.1 / §3.3: route the popup's "View history" click through the
    // background script, which raises the desktop dashboard's History
    // tab via native messaging. If the native host is unavailable we
    // surface a single-line install hint right under the link.
    const handleOpenDashboardHistory = useCallback(() => {
        setHistoryStatus("");
        safeSendMessage(
            { type: "OPEN_DASHBOARD_HISTORY" },
            (raw) => {
                const resp = raw as { status?: string } | undefined;
                if (resp?.status === "unavailable") {
                    setHistoryStatus(
                        "Install the Cortex desktop app to view history.",
                    );
                    // Hide the hint after a beat so it doesn't loiter.
                    setTimeout(() => setHistoryStatus(""), 8000);
                } else {
                    setHistoryStatus("");
                }
            },
        );
    }, []);

    // P0 §3.3: clear the cached recap and badge, then drop the card
    // locally so the user gets immediate feedback.
    const handleDismissRecap = useCallback(() => {
        safeSendMessage({ type: "DISMISS_RECAP" });
        setRecap(null);
        setRecapTimestamp(null);
    }, []);

    // Phase 4d Task H / §3.24: in-app bug report submitter. POSTs to the
    // daemon's ``/api/feedback`` endpoint (added by Phase 4b-retry-1);
    // on 404 we stash the payload in chrome.storage.local under
    // ``pending_feedback`` so a future popup mount can retry. Anything
    // else (network error, 5xx) becomes a generic error state the user
    // can retry from.
    const handleBugReportSubmit = useCallback(async () => {
        const description = bugReportText.trim();
        if (description.length < 10 || description.length > 500) {
            setBugReportError("Description must be 10-500 characters.");
            return;
        }
        setBugReportStatus("submitting");
        setBugReportError("");
        // C2: FeedbackRequest carries both ``user_agent`` (browser UA) and
        // ``app_version`` (the extension manifest version). The gateway
        // persists both in the stored feedback record.
        let appVersion = "";
        try {
            appVersion = chrome?.runtime?.getManifest?.()?.version ?? "";
        } catch { /* chrome.runtime not available in some test envs */ }
        const body = {
            description,
            include_logs: bugReportIncludeLogs,
            user_agent: typeof navigator !== "undefined"
                ? navigator.userAgent
                : "",
            app_version: appVersion,
            timestamp: Date.now() / 1000,
        };
        try {
            // C1: /api/feedback is capability-token-gated. Attach the cached
            // local token or the daemon 401s. Token-fetch failure is
            // non-fatal — the POST still goes out and 401s cleanly.
            let authToken: string | null = null;
            try {
                authToken = await getAuthToken();
            } catch { /* native host unavailable; request 401s cleanly */ }
            const resp = await fetch(`${DAEMON_HTTP_URL}/api/feedback`, {
                method: "POST",
                headers: authToken
                    ? {
                          "Content-Type": "application/json",
                          "X-Cortex-Auth-Token": authToken,
                      }
                    : { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (resp.ok) {
                setBugReportStatus("saved");
                setBugReportText("");
                setTimeout(() => {
                    setBugReportOpen(false);
                    setBugReportStatus("idle");
                }, 1500);
                return;
            }
            if (resp.status === 404) {
                // Endpoint not present yet — queue locally. Cap at
                // ``PENDING_FEEDBACK_MAX`` most-recent entries on every
                // write so the queue can never grow unboundedly across
                // months of offline use.
                const existing = (((await chrome.storage.local.get(
                    "pending_feedback",
                )).pending_feedback as unknown[]) || []);
                const recent = [...existing, body].slice(-PENDING_FEEDBACK_MAX);
                await chrome.storage.local.set({ pending_feedback: recent });
                setBugReportStatus("queued");
                setBugReportText("");
                return;
            }
            setBugReportStatus("error");
            setBugReportError(`Server returned ${resp.status}.`);
        } catch (err) {
            // Network failure — queue locally as well so the user
            // doesn't lose the report. Same recent-N bound applies.
            try {
                const existing = (((await chrome.storage.local.get(
                    "pending_feedback",
                )).pending_feedback as unknown[]) || []);
                const recent = [...existing, body].slice(-PENDING_FEEDBACK_MAX);
                await chrome.storage.local.set({ pending_feedback: recent });
                setBugReportStatus("queued");
                setBugReportText("");
            } catch {
                setBugReportStatus("error");
                setBugReportError(
                    err instanceof Error ? err.message : String(err),
                );
            }
        }
    }, [bugReportText, bugReportIncludeLogs]);

    // Phase 4d Task H / §3.24 hardening: on popup mount, drain the
    // ``pending_feedback`` queue against the daemon's ``/api/feedback``
    // endpoint. Each item that re-POSTs successfully is dropped; the
    // rest stay queued for the next mount. The queue is hard-capped at
    // ``PENDING_FEEDBACK_MAX`` at the end of every drain so a long
    // string of failures cannot bloat ``chrome.storage.local``.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            let queue: unknown[];
            try {
                queue = (((await chrome.storage.local.get(
                    "pending_feedback",
                )).pending_feedback as unknown[]) || []);
            } catch {
                return;
            }
            if (!Array.isArray(queue) || queue.length === 0) return;
            // C1: the drain re-POSTs to the token-gated /api/feedback. Fetch
            // the token once for the whole drain; without it every queued
            // item 401s and is re-queued forever.
            let authToken: string | null = null;
            try {
                authToken = await getAuthToken();
            } catch { /* native host unavailable; items stay queued */ }
            const drainHeaders: Record<string, string> = authToken
                ? {
                      "Content-Type": "application/json",
                      "X-Cortex-Auth-Token": authToken,
                  }
                : { "Content-Type": "application/json" };
            const remaining: unknown[] = [];
            for (const item of queue) {
                if (cancelled) {
                    remaining.push(item);
                    continue;
                }
                try {
                    const resp = await fetch(`${DAEMON_HTTP_URL}/api/feedback`, {
                        method: "POST",
                        headers: drainHeaders,
                        body: JSON.stringify(item),
                    });
                    if (!resp.ok) remaining.push(item);
                } catch {
                    remaining.push(item);
                }
            }
            if (cancelled) return;
            try {
                const capped = remaining.slice(-PENDING_FEEDBACK_MAX);
                await chrome.storage.local.set({ pending_feedback: capped });
            } catch { /* storage write failed — leave queue untouched */ }
        })();
        return () => { cancelled = true; };
    }, []);

    // P0 §3.3 hardening: the recap card's 24h TTL check at render time
    // hides a stale card on the next paint, but if the popup is left
    // open for >24h (uncommon but possible on a pinned tab / dev
    // window) the card lingers because nothing re-renders. Arm a
    // setTimeout the moment we adopt a recap so it auto-dismisses
    // exactly when the TTL crosses. ``handleDismissRecap`` is stable
    // via useCallback([]), so this effect re-arms only when the
    // recap timestamp actually changes.
    useEffect(() => {
        if (recap == null || recapTimestamp == null) return;
        const elapsed = Date.now() - recapTimestamp;
        const remaining = RECAP_TTL_MS - elapsed;
        if (remaining <= 0) {
            // Already past TTL — dismiss synchronously rather than
            // arming a zero-delay timer.
            handleDismissRecap();
            return;
        }
        const handle = setTimeout(handleDismissRecap, remaining);
        return () => clearTimeout(handle);
    }, [recap, recapTimestamp, handleDismissRecap]);

    const [stopping, setStopping] = useState(false);
    const handleStopCortex = useCallback(async () => {
        setStopping(true);
        // Force local UI to disconnected immediately
        setConnected(false);
        setState(null);
        setFocus(null);
        // Tell background to disconnect WS, kill daemon via HTTP, close tabs
        sendWithCid({ type: "STOP_CORTEX" });
        // Wait a moment for shutdown to propagate, then release button
        setTimeout(() => setStopping(false), 2000);
    }, []);

    const handleStartFocus = useCallback(() => {
        const goal = goalInput.trim();
        if (goal === "") {
            return;
        }
        sendWithCid({ type: "START_FOCUS", goal });
        setGoalInput("");
    }, [goalInput]);

    const handleStopFocus = useCallback(() => {
        sendWithCid({ type: "STOP_FOCUS" });
    }, []);

    // Derived
    const stateStr = state?.state ?? "";
    const stateColor = STATE_COLORS[stateStr] || CX.textTertiary;
    // ``Idle`` only when we've actually received a STATE_UPDATE whose
    // ``state`` field is missing/unknown. Pre-first-frame (the WS has
    // opened but the daemon hasn't broadcast yet because AUTH is still
    // round-tripping the native host) shows ``Connecting…`` instead —
    // ``Idle`` would mis-attribute the wait to the user being inactive.
    const stateLabel = state
        ? STATE_LABELS[stateStr] || "Idle"
        : connected
            ? "Connecting…"
            : "Idle";
    const hr = state?.biometrics?.heart_rate;
    const hrv = state?.biometrics?.hrv_rmssd;
    const blink = state?.biometrics?.blink_rate;
    // Capture-pipeline status mirrored from the daemon (set in
    // ``WebSocketServer._make_state_update``). Drives the
    // "Camera offline / Looking for your face / Reading your pulse"
    // banner shown in the BPM/HRV/BLK area when no HR is available.
    // ``state`` may be null pre-first-STATE_UPDATE; default both flags
    // to ``true`` so the most benign message ("Reading your pulse…")
    // wins until we hear otherwise — same fallback as the desktop tab.
    // F5 (Phase-4 audit): ``capture`` is now part of the CortexState
    // interface, so no double cast is needed — TS narrows the optional
    // field directly.
    const captureRaw = state?.capture;
    const framesFlowing = captureRaw?.frames_flowing ?? true;
    const faceDetected = captureRaw?.face_detected ?? true;
    // F16: ``capture.stale`` and ``store.degraded`` are the two
    // health-warning surfaces the daemon raises at the envelope level.
    const captureStale = captureRaw?.stale === true;
    const storeDegraded = state?.store?.degraded === true;
    const bioStatusMessage = !state
        ? "Connecting to daemon…"
        : !framesFlowing
            ? "Camera offline — open System Settings → Privacy & Security → Camera"
            : !faceDetected
                ? "Looking for your face…"
                : "Reading your pulse…";

    const focusMin = focus ? Math.round(focus.focusMs / 60000) : 0;
    const elapsedMin = focus ? Math.round(focus.elapsedMs / 60000) : 0;
    const streakSec = focus ? Math.round(focus.currentStreakMs / 1000) : 0;
    const streakMin = Math.floor(streakSec / 60);
    const streakRemSec = streakSec % 60;

    const genericReasonPhrases = ["not essential for", "not relevant to", "not related to",
        "may be distracting", "could be a distraction", "is a distraction", "not needed for",
        "distracting you from", "not useful for"];
    const closeTabs = tabRecs?.tabs?.filter(t => t.action === "close" || t.action === "bookmark_and_close") || [];
    const keepTabs = tabRecs?.tabs?.filter(t => t.action === "keep") || [];
    const rec = activeActions.filter(a => a.category === "recommended");

    const visibleCloseTabs = tabsExpanded ? closeTabs : closeTabs.slice(0, 5);
    const overflowCount = tabsExpanded ? 0 : closeTabs.length - visibleCloseTabs.length;

    const genericErrPhrases = ["no specific errors", "no errors detected", "not applicable", "no error", "n/a"];
    const realErrAnalysis = errAnalysis?.root_cause && !genericErrPhrases.some(
        p => (errAnalysis.root_cause ?? "").toLowerCase().includes(p)
    ) ? errAnalysis : null;

    const realCausal = causalExplanation && causalExplanation.length > 20
        && /\d/.test(causalExplanation) ? causalExplanation : "";

    const hasIntervention =
        activeActions.length > 0 ||
        tabRecs ||
        realErrAnalysis ||
        microSteps.length > 0 ||
        // P1-FC: a failure banner or an active cross-surface prompt must
        // keep the card on screen even if no actionable items remain.
        interventionError !== null ||
        interventionPrompt !== null;

    // P0 §3.6: optimistic-toggle handler. The handler updates local
    // state immediately so the user sees the strikethrough flip without
    // waiting for the daemon round-trip, then dispatches the relay
    // message; the daemon's rebroadcast will reconcile the authoritative
    // status into ``microSteps`` on the next INTERVENTION_TRIGGER.
    const handleMicroStepToggle = (idx: number, checked: boolean) => {
        if (!interventionId) return;
        const newStatus: "pending" | "done" = checked ? "done" : "pending";
        setMicroSteps(prev => prev.map(
            (s, i) => i === idx ? { ...s, status: newStatus } : s
        ));
        safeSendMessage({
            type: "MICRO_STEP_TOGGLED",
            intervention_id: interventionId,
            step_index: idx,
            new_status: newStatus,
        });
    };

    return (
        <div style={S.root}>
            {/* Alert toast — top-right, auto-dismiss 10s */}
            {alert && (
                <div style={S.alertBox}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                        <div style={S.alertTitle}>{alert.title}</div>
                        <button
                            style={{ background: "none", border: "none", color: CX.textTertiary, cursor: "pointer", fontSize: 13, padding: 0, fontFamily: CX.font, lineHeight: 1 }}
                            onClick={() => setAlert(null)}
                        >{"\u00d7"}</button>
                    </div>
                    <div style={S.alertBody}>{alert.body}</div>
                </div>
            )}

            {/* Header — 44px total, 20px horizontal, 12px vertical */}
            <div style={S.header}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <CortexLogo />
                    <span style={{ ...S.logoText, fontFamily: CX.fontBrand, fontStyle: "italic", letterSpacing: "0.02em" }}>Cortex.</span>
                </div>
                {!connected ? (
                    <button style={S.connectBtn} onClick={handleConnect}>CONNECT</button>
                ) : (
                    <div style={S.statusRow} aria-live="polite">
                        <div style={getStateDotStyle(stateStr, stateColor)} />
                        <span style={{ ...S.statusLabel, color: stateColor }}>{stateLabel}</span>
                    </div>
                )}
            </div>

            {/* F16 (Phase-4 audit): envelope-level health warning
                strip. The daemon stamps ``capture.stale=true`` when the
                webcam loop has not produced a frame in the staleness
                window, and ``store.degraded=true`` when SQLite has
                fallen back to read-only / journal-only mode. Surface
                the most-severe single message at the top of the popup
                so the user can take action (reopen permissions, kill
                a conflicting process, etc). Uses the existing danger
                tint from design-tokens — no new palette.
                aria-live="polite" so screen readers announce it
                without interrupting other speech. */}
            {connected && (captureStale || storeDegraded) && (
                <div
                    role="status"
                    aria-live="polite"
                    data-testid="cortex-health-banner"
                    style={{
                        margin: "0 16px 8px",
                        padding: "6px 10px",
                        borderRadius: CX.radiusSm,
                        background: "rgba(215, 0, 21, 0.10)",
                        border: `1px solid ${CX.danger}`,
                        color: CX.danger,
                        fontSize: 11,
                        fontFamily: CX.font,
                        lineHeight: 1.35,
                    }}
                >
                    {captureStale
                        ? "Camera offline — frames are not flowing"
                        : "Storage degraded — sessions may not persist"}
                </div>
            )}

            {/* P0 §3.3: end-of-session recap card. Sits at the top of
                the popup body so it's the first thing the user sees on
                their next open. ``recapValid`` enforces the 24h TTL —
                a recap older than that is hidden even if it lingers in
                chrome.storage.local. */}
            {recap && (recapTimestamp == null || Date.now() - recapTimestamp <= RECAP_TTL_MS) && (() => {
                const r = recap;
                const durationMin = Math.round((r.duration_seconds ?? 0) / 60);
                const flowPct = Math.round(r.flow_percentage ?? 0);
                const breaks = r.breaks_taken ?? 0;
                // P0 §3.3 hardening: renamed from ``streakMin`` so it
                // doesn't shadow the outer focus-session ``streakMin``
                // (declared above for the live STREAK stat). Two
                // distinct concepts — the recap card's longest flow
                // streak vs. the running session's current streak —
                // should not share a name inside the same render scope.
                const recapStreakMin = Math.round(
                    (r.longest_flow_streak_seconds ?? 0) / 60,
                );
                const hr = r.avg_hr_bpm;
                return (
                    <div style={S.recapCard} data-testid="recap-card">
                        <div style={S.recapHeaderRow}>
                            <div style={S.recapHeadline}>
                                Session ended {"·"} {durationMin}m
                            </div>
                            <button
                                aria-label="Dismiss recap"
                                style={S.recapDismissIcon}
                                onClick={handleDismissRecap}
                            >{"×"}</button>
                        </div>
                        <div style={S.recapBody}>
                            {flowPct}% in flow {"·"} {breaks} break
                            {breaks === 1 ? "" : "s"} {"·"} longest
                            streak {recapStreakMin}m
                        </div>
                        {recapPersisted === false && (
                            // C4: the broadcast raced ahead of the atomic
                            // disk write. Signal that "View on desktop" may
                            // briefly lag until the session file lands.
                            <div
                                style={S.recapStat}
                                data-testid="recap-not-persisted"
                            >
                                Saving to history…
                            </div>
                        )}
                        {hr != null && (
                            // P0 §3.3 hardening: the schema field is
                            // ``avg_hr_bpm`` (mean across the session,
                            // not peak). The previous "Peak HR" copy
                            // misrepresented the number; "Avg HR"
                            // matches the recap_sheet.py relabel in
                            // the desktop shell.
                            <div style={S.recapStat}>
                                Avg HR {Math.round(hr)} bpm
                            </div>
                        )}
                        <div style={S.recapButtonRow}>
                            <button
                                style={S.recapPrimaryBtn}
                                onClick={handleOpenDashboardHistory}
                                data-testid="recap-view-on-desktop"
                            >View on desktop {"→"}</button>
                            <button
                                style={S.recapGhostBtn}
                                onClick={handleDismissRecap}
                                data-testid="recap-dismiss"
                            >Dismiss</button>
                        </div>
                    </div>
                );
            })()}

            {/* Morning briefing — below header, before session card */}
            {briefing && (
                <div style={S.briefingCard}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                        <div>
                            <div style={S.briefingTitle}>Where you left off</div>
                            <div style={S.briefingBody}>{briefing.summary}</div>
                        </div>
                        <button
                            style={{ background: "none", border: "none", color: CX.textTertiary, cursor: "pointer", fontSize: 13, padding: 0, fontFamily: CX.font, lineHeight: 1, flexShrink: 0, marginLeft: 8 }}
                            onClick={() => setBriefing(null)}
                        >{"\u00d7"}</button>
                    </div>
                    <div style={{ marginTop: 8 }}>
                        <button style={S.ghostBtn} onClick={() => {
                            const leftOff = (briefing.left_off_at ?? "").trim();
                            if (leftOff !== "") {
                                sendWithCid({ type: "START_FOCUS", goal: leftOff });
                            }
                            setBriefing(null);
                        }}>Resume</button>
                    </div>
                </div>
            )}

            {/* F54: render the diagnostic block whenever we're not fully ok.
                 installed_version_mismatch and handshake_failed both happen
                 while `connected` is true, so the visibility predicate is
                 the resolved connectivity enum rather than the bare flag. */}
            {connectivity !== "ok" && (
                <div style={S.disconnectedArea}>
                    <div style={{
                        width: 40,
                        height: 40,
                        borderRadius: "50%",
                        border: `1.5px solid ${launching ? CX.accent : CX.textTertiary}`,
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        transition: `border-color ${CX.durationSlow} ${CX.easeDefault}`,
                        marginBottom: 12,
                    }}>
                        {launching ? (
                            <div style={{
                                width: 8,
                                height: 8,
                                borderRadius: "50%",
                                background: CX.accent,
                                animation: "cxPulse 1.5s ease-in-out infinite",
                            }} />
                        ) : (
                            <div style={{
                                width: 0,
                                height: 0,
                                borderLeft: `8px solid ${CX.textTertiary}`,
                                borderTop: "5px solid transparent",
                                borderBottom: "5px solid transparent",
                                marginLeft: 2,
                            }} />
                        )}
                    </div>
                    {(() => {
                        // F54: pick title/body/CTA per distinct connectivity state.
                        let title: string;
                        let body: string;
                        let ctaLabel: string;
                        let ctaHandler: () => void = handleLaunchCortex;
                        let testId: string;
                        if (launching) {
                            title = "Starting Cortex";
                            body = launchStatus || "Launching daemon\u2026";
                            ctaLabel = "Starting\u2026";
                            testId = "conn-state-launching";
                        } else if (connectivity === "not_installed") {
                            title = "Native host not installed";
                            body = "Cortex needs its native messaging host registered. Run `python -m cortex.scripts.install_native_host` once, then relaunch your browser.";
                            ctaLabel = "Open install instructions";
                            ctaHandler = () => {
                                chrome.tabs.create({ url: chrome.runtime.getURL("tabs/onboarding.html") });
                            };
                            testId = "conn-state-not_installed";
                        } else if (connectivity === "installed_version_mismatch") {
                            title = "Daemon version mismatch";
                            body = `Extension expects v${EXPECTED_VERSION}; daemon is v${daemonVersion ?? "?"}. Update the daemon or downgrade the extension to match.`;
                            ctaLabel = "Restart daemon";
                            ctaHandler = handleLaunchCortex;
                            testId = "conn-state-installed_version_mismatch";
                        } else if (connectivity === "handshake_failed") {
                            title = "Handshake failed";
                            body = handshakeError || "The daemon answered but rejected this extension's handshake. Check the local auth token.";
                            ctaLabel = "Retry handshake";
                            ctaHandler = () => sendWithCid({ type: "CONNECT" });
                            testId = "conn-state-handshake_failed";
                        } else {
                            // installed_no_daemon (default disconnect path)
                            title = "Not connected";
                            body = launchStatus || "Launch daemon with camera";
                            ctaLabel = launchError ? "Retry" : "Start Cortex";
                            testId = "conn-state-installed_no_daemon";
                        }
                        return (
                            <>
                                <div style={S.disconnectedTitle} data-testid={testId}>{title}</div>
                                <div style={S.disconnectedBody}>{body}</div>
                                <button
                                    style={{
                                        ...S.primaryBtn,
                                        marginTop: 16,
                                        opacity: launching ? 0.5 : 1,
                                        pointerEvents: launching ? "none" as const : "auto" as const,
                                        maxWidth: 240,
                                    }}
                                    onClick={ctaHandler}
                                    disabled={launching}
                                >
                                    {ctaLabel}
                                </button>
                            </>
                        );
                    })()}
                    {launchError && launchStatus && (
                        <div style={{
                            fontSize: 10,
                            color: CX.textTertiary,
                            fontFamily: CX.mono,
                            marginTop: 8,
                            textAlign: "center" as const,
                            lineHeight: 1.5,
                            maxWidth: 280,
                            wordBreak: "break-word" as const,
                        }}>
                            {launchStatus}
                        </div>
                    )}
                </div>
            )}

            {/* Goal input — one input, Enter to start, no separate button */}
            {connected && !focus && (
                <div style={{ marginBottom: CX.space6, position: "relative" as const }}>
                    <input
                        className="cortex-goal-input"
                        style={S.goalInput}
                        placeholder="What are you working on?"
                        value={goalInput}
                        // F15 (Phase-4 audit): cap at 500 chars so the
                        // text never breaches the daemon's GoalSet
                        // schema upper bound. The visual input is sized
                        // for ~80 chars but pasted content used to slip
                        // through unbounded.
                        maxLength={500}
                        onChange={(e) => setGoalInput(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && handleStartFocus()}
                    />
                    <span style={S.goalEnterIcon}>{"\u23CE"}</span>
                </div>
            )}

            {/* Active focus session — sticky */}
            {focus && (
                <div style={{ ...S.sessionCard, position: "sticky" as const, top: 0, zIndex: 10 }}>
                    {/* First row: "Study session · Xm" + End */}
                    <div style={S.focusHeader}>
                        <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
                            <span style={S.focusTitle}>{focus.goal}</span>
                            <span style={S.focusDuration}>{"\u00b7"} {elapsedMin}m</span>
                        </div>
                        <button style={S.endBtn} onClick={handleStopFocus}>End</button>
                    </div>
                    {/* Phase-3 / Audit-1.2 F3: auto-armed indicator pill */}
                    {focus.autoArmed && (
                        <div
                            role="status"
                            style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 6,
                                marginTop: 6,
                                padding: "3px 10px",
                                fontSize: 11,
                                fontFamily: CX.font,
                                fontWeight: 500,
                                color: CX.accent,
                                background: "rgba(217,119,87,0.16)",
                                border: "1px solid rgba(217,119,87,0.36)",
                                borderRadius: CX.radiusMd,
                                width: "fit-content",
                            }}
                            aria-label={"Auto-armed focus protection, preset " + (focus.preset || "developer")}
                        >
                            <span aria-hidden="true">{"\u25cf"}</span>
                            {" Auto-armed"}
                            {focus.preset && (
                                <span style={{ opacity: 0.78 }}>
                                    {" \u00b7 " + focus.preset}
                                </span>
                            )}
                        </div>
                    )}

                    {/* Big number + percentage on same baseline */}
                    <div style={S.bigRow}>
                        <span style={{ ...S.bigNum, color: stateColor }}>{focusMin}</span>
                        <span style={S.bigPct}>{focus.focusPct}%</span>
                    </div>
                    <div style={S.bigLabel}>min focused</div>

                    {/* Progress bar — 6px tall */}
                    <div style={S.trackOuter}>
                        <div style={{
                            ...S.trackFill,
                            width: `${Math.max(Math.min(focus.focusPct, 100), 0)}%`,
                            minWidth: 6,
                            background: stateColor,
                        }} />
                    </div>

                    {/* Stats row — three columns, center-aligned */}
                    <div style={S.statsRow}>
                        <div style={S.statCol}>
                            <span style={S.statVal}>{streakMin > 0 ? `${streakMin}:${String(streakRemSec).padStart(2, "0")}` : `${streakSec}s`}</span>
                            <span style={{ ...S.statLabel, fontWeight: 500 }}>STREAK</span>
                        </div>
                        <div style={S.statCol}>
                            <span style={S.statVal}>{focus.distractionsBlocked}</span>
                            <span style={S.statLabel}>BLOCKED</span>
                        </div>
                        <div style={S.statCol}>
                            <span style={S.statVal}>{focus.longestStreakMin}m</span>
                            <span style={S.statLabel}>BEST</span>
                        </div>
                    </div>
                </div>
            )}

            {/* P0 §3.7: BREAK_RECOMMENDATION pill. Surfaces above the
                intervention card whenever the daemon's stress integral
                crosses threshold. Single CTA dispatches the bound
                ``take_biology_break`` action through the EXECUTE_ACTION
                channel. */}
            {breakRec && (
                <div
                    data-testid="break-recommendation-pill"
                    style={{
                        background: "rgba(217, 119, 87, 0.10)",
                        border: `1px solid ${CX.accent}55`,
                        borderRadius: CX.radiusMd,
                        padding: "10px 12px",
                        marginBottom: 10,
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                        fontFamily: CX.font,
                    }}
                >
                    <div style={{ flex: 1, fontSize: 12, color: CX.text }}>
                        Your HRV has been suppressed — take a {Math.round(breakRec.duration_seconds / 60)}-minute break?
                    </div>
                    <button
                        style={{
                            padding: "6px 12px",
                            border: "none",
                            borderRadius: CX.radiusSm,
                            background: CX.accent,
                            color: "white",
                            fontSize: 11,
                            fontWeight: 600,
                            cursor: "pointer",
                            fontFamily: CX.font,
                        }}
                        data-testid="break-recommendation-cta"
                        onClick={() => {
                            safeSendMessage({
                                type: "EXECUTE_ACTION",
                                action: {
                                    action_id: `bk_${Date.now()}`,
                                    action_type: "take_biology_break",
                                    label: "Take a break",
                                    target: "",
                                    metadata: {
                                        duration_seconds: breakRec.duration_seconds,
                                        breathing_pattern: breakRec.breathing_pattern,
                                        audio_cue: true,
                                        reason: breakRec.reason,
                                    },
                                },
                                intervention_id: interventionId || `break_${Date.now()}`,
                            });
                            setBreakRec(null);
                        }}
                    >
                        Take {Math.round(breakRec.duration_seconds / 60)} min
                    </button>
                    <button
                        aria-label="Dismiss break recommendation"
                        style={{
                            border: "none",
                            background: "transparent",
                            color: CX.textSecondary,
                            cursor: "pointer",
                            fontSize: 14,
                        }}
                        onClick={() => setBreakRec(null)}
                    >
                        {"×"}
                    </button>
                </div>
            )}

            {/* Intervention preview */}
            {hasIntervention && (
                <div style={S.interventionCard}>
                    {/* P1-FC-INTERVENTION-FAILED: total-mutation-failure
                        banner. The daemon reported the workspace was NOT
                        changed; the apply CTA below is disabled while this
                        is set so the user isn't told to engage a broken
                        plan. */}
                    {interventionError && (
                        <div
                            data-testid="intervention-error-banner"
                            role="alert"
                            style={{
                                marginBottom: 12,
                                padding: "8px 10px",
                                background: "rgba(228, 122, 110, 0.12)",
                                border: "1px solid rgba(228, 122, 110, 0.4)",
                                borderRadius: CX.radiusSm,
                                color: "#E47A6E",
                                fontSize: 12,
                                fontFamily: CX.font,
                                lineHeight: 1.4,
                            }}
                        >
                            {interventionError}
                        </div>
                    )}

                    {/* P1-FC-INTERVENTION-PROMPT: informational prompt text
                        relayed cross-surface (micro-commit / movement
                        break). Rendered above the action card so a
                        popup-open user has awareness of the active prompt. */}
                    {interventionPrompt && (
                        <div
                            data-testid="intervention-prompt"
                            style={{
                                marginBottom: 12,
                                padding: "8px 10px",
                                background: "rgba(255, 255, 255, 0.04)",
                                borderRadius: CX.radiusSm,
                                color: CX.textSecondary,
                                fontSize: 12,
                                fontFamily: CX.font,
                                lineHeight: 1.4,
                            }}
                        >
                            {interventionPrompt}
                        </div>
                    )}

                    {/* Causal explanation */}
                    {realCausal && (
                        <div style={S.causalText}>{realCausal}</div>
                    )}

                    {/* P0 §3.9: "Why?" drilldown. Shows the structured
                        causal signals (top 2-3) as sparkline rows on
                        expansion. The drilldown is opt-in — collapsed
                        by default behind a small chevron link. */}
                    {(causalSignals.length > 0 || realCausal) && (
                        <div
                            style={{ marginBottom: 10 }}
                            data-testid="why-drilldown"
                        >
                            <button
                                aria-label="Show structured causal rationale"
                                onClick={() => {
                                    if (!whyOpen && causalSignals.length === 0 && interventionId) {
                                        safeSendMessage({
                                            type: "WHY_DETAIL_REQUEST",
                                            intervention_id: interventionId,
                                        });
                                    }
                                    setWhyOpen(!whyOpen);
                                }}
                                style={{
                                    background: "none",
                                    border: "none",
                                    color: CX.textSecondary,
                                    fontSize: 10,
                                    fontFamily: CX.font,
                                    cursor: "pointer",
                                    padding: 0,
                                    textDecoration: "underline",
                                }}
                                data-testid="why-toggle"
                            >
                                {whyOpen ? "Hide why" : "Why?"}
                            </button>
                            {whyOpen && whyError && (
                                <div
                                    data-testid="why-error"
                                    style={{
                                        marginTop: 6,
                                        padding: "6px 10px",
                                        background: "rgba(255, 255, 255, 0.03)",
                                        borderRadius: CX.radiusSm,
                                        color: CX.textSecondary,
                                        fontSize: 10,
                                        fontFamily: CX.font,
                                        fontStyle: "italic",
                                    }}
                                >
                                    Cause data temporarily unavailable: {whyError}
                                </div>
                            )}
                            {whyOpen && causalSignals.length > 0 && (
                                <div
                                    style={{
                                        marginTop: 6,
                                        padding: "8px 10px",
                                        background: "rgba(255, 255, 255, 0.03)",
                                        borderRadius: CX.radiusSm,
                                    }}
                                    data-testid="why-rows"
                                >
                                    {causalSignals.map((sig, idx) => {
                                        const isPrimary = sig.severity === "primary";
                                        const delta = sig.delta_pct;
                                        const arrow = delta == null ? "" : delta < 0 ? "↓" : "↑";
                                        return (
                                            <div
                                                key={`${sig.name}-${idx}`}
                                                style={{
                                                    display: "flex",
                                                    alignItems: "center",
                                                    gap: 8,
                                                    padding: "4px 0",
                                                    fontSize: 11,
                                                    color: CX.text,
                                                    fontFamily: CX.font,
                                                }}
                                            >
                                                <span
                                                    style={{
                                                        fontWeight: isPrimary ? 600 : 500,
                                                        minWidth: 80,
                                                    }}
                                                >
                                                    {sig.name}
                                                </span>
                                                <span style={{ flex: 1, color: CX.textSecondary }}>
                                                    {sig.current_value.toFixed(1)}{sig.unit}
                                                    {sig.baseline_value != null && (
                                                        <span style={{ marginLeft: 4 }}>
                                                            (baseline {sig.baseline_value.toFixed(1)}{sig.unit})
                                                        </span>
                                                    )}
                                                </span>
                                                {delta != null && (
                                                    <span
                                                        style={{
                                                            color: delta < 0 ? "#E47A6E" : CX.accent,
                                                            fontWeight: 600,
                                                            fontSize: 10,
                                                        }}
                                                    >
                                                        {arrow}{Math.abs(delta).toFixed(0)}%
                                                    </span>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    )}

                    {/* P0 §3.6: micro-step checklist. Each click sends
                        MICRO_STEP_TOGGLED via background.ts → daemon WS.
                        The daemon mutates the active plan and rebroadcasts
                        INTERVENTION_TRIGGER with the new status. */}
                    {microSteps.length > 0 && (
                        <div
                            data-testid="micro-step-list"
                            style={{ marginBottom: 12 }}
                        >
                            {microSteps.map((step, idx) => {
                                const isDone = step.status === "done";
                                return (
                                    <label
                                        key={`ms-${idx}`}
                                        data-testid={`micro-step-row-${idx}`}
                                        style={{
                                            display: "flex",
                                            alignItems: "center",
                                            gap: 8,
                                            padding: "4px 0",
                                            cursor: "pointer",
                                            fontSize: 12,
                                            color: isDone ? CX.textSecondary : CX.text,
                                            fontFamily: CX.font,
                                            textDecoration: isDone ? "line-through" : "none",
                                            opacity: isDone ? 0.7 : 1,
                                        }}
                                    >
                                        <input
                                            type="checkbox"
                                            data-testid={`micro-step-checkbox-${idx}`}
                                            checked={isDone}
                                            onChange={(e) => handleMicroStepToggle(idx, e.target.checked)}
                                            style={{ accentColor: CX.accent, width: 14, height: 14 }}
                                        />
                                        <span>{step.text}</span>
                                    </label>
                                );
                            })}
                        </div>
                    )}

                    {visibleCloseTabs.length > 0 && (
                        <div style={{ marginBottom: 12 }}>
                            {visibleCloseTabs.map((t, i) => {
                                const title = String(t.tab_title || "Untitled");
                                const rawReason = String(t.reason || "");
                                const reason = genericReasonPhrases.some(p => rawReason.toLowerCase().includes(p)) ? "" : rawReason;
                                return (
                                    <div key={`c${i}`} style={S.tabRow}>
                                        <span style={S.tabXMark}>{"\u2715"}</span>
                                        <span style={S.tabName}>{title}</span>
                                    </div>
                                );
                            })}
                            {overflowCount > 0 && (
                                <button
                                    style={{ fontSize: 10, color: CX.accent, marginTop: 4, background: "none", border: "none", cursor: "pointer", padding: 0, fontFamily: CX.font }}
                                    onClick={() => setTabsExpanded(true)}
                                >+{overflowCount} more</button>
                            )}
                            {keepTabs.length > 0 && (
                                <div style={S.keepLine}>Keeping {keepTabs.length} you need</div>
                            )}
                        </div>
                    )}

                    {realErrAnalysis && realErrAnalysis.root_cause && (
                        <div style={S.errBox}>
                            <div style={S.errBody}>{realErrAnalysis.root_cause}</div>
                            {realErrAnalysis.suggested_fix && (
                                <pre style={S.errCode}>{"\u2192 "}{realErrAnalysis.suggested_fix}</pre>
                            )}
                        </div>
                    )}

                    {!tabRecs && !realErrAnalysis && rec.length > 0 && (
                        <div style={{ marginBottom: 12 }}>
                            {rec.map((a, i) => (
                                <div key={i} style={S.tabRow}>
                                    <span style={{ ...S.tabXMark, color: CX.textSecondary }}>{"\u2022"}</span>
                                    <span style={{ ...S.tabName, color: CX.text }}>{String(a.label || "")}</span>
                                </div>
                            ))}
                        </div>
                    )}

                    {/* Summary + single CTA */}
                    {rec.length > 0 && (
                        <>
                            <button
                                style={
                                    applied || interventionError
                                        ? { ...S.primaryBtn, ...S.doneBtnStyle }
                                        : S.primaryBtn
                                }
                                disabled={applied || interventionError !== null}
                                onClick={() => {
                                    sendWithCid(
                                        {
                                            type: "EXECUTE_ALL_RECOMMENDED",
                                            actions: rec,
                                            intervention_id: interventionId,
                                        },
                                        (raw: unknown) => {
                                            const results = raw as Array<{ success: boolean }> | undefined;
                                            const succeeded = Array.isArray(results) && results.some(r => r.success);
                                            if (succeeded) {
                                                setApplied(true);
                                                setTimeout(() => {
                                                    setActiveActions([]);
                                                    setTabRecs(null);
                                                    setErrAnalysis(null);
                                                    setApplied(false);
                                                }, 10000);
                                            } else {
                                                setApplied(true);
                                            }
                                        },
                                    );
                                }}
                            >
                                {interventionError
                                    ? "Couldn't apply"
                                    : applied
                                    ? "Done"
                                    : closeTabs.length > 0
                                        ? `Close ${closeTabs.length} tab${closeTabs.length !== 1 ? "s" : ""}`
                                        : errAnalysis
                                            ? "Help me fix this"
                                            : `Apply ${rec.length} change${rec.length !== 1 ? "s" : ""}`}
                            </button>
                            {applied && (
                                <div style={S.undoRow}>
                                    <span>Done.</span>
                                    <button
                                        style={S.undoLink}
                                        onClick={() => {
                                            sendWithCid(
                                                { type: "UNDO_ALL_RECENT", intervention_id: interventionId },
                                                () => setApplied(false),
                                            );
                                        }}
                                    >Undo</button>
                                </div>
                            )}
                        </>
                    )}

                    {/* P0 §3.8: rating row — surfaces after action click
                        or 30 s, whichever comes first. "Not helpful"
                        reveals an optional one-line text input the user
                        can skip with Enter. Rating + text are routed via
                        background.ts → USER_RATING WS frame.
                        Spec line 710: only show on guided_mode +
                        simplified_workspace to keep minimal-tone
                        overlays ambient. */}
                    {(interventionLevel === "guided_mode"
                        || interventionLevel === "simplified_workspace")
                        && (applied || rating !== null || ratingTextOpen) && (
                        <div
                            data-testid="rating-row"
                            style={{
                                marginTop: 12,
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                justifyContent: "center",
                            }}
                        >
                            <button
                                data-testid="rating-thumbs-up"
                                aria-label="Mark helpful"
                                aria-pressed={rating === "thumbs_up"}
                                onClick={() => {
                                    if (!interventionId) return;
                                    setRating("thumbs_up");
                                    safeSendMessage({
                                        type: "USER_RATING",
                                        intervention_id: interventionId,
                                        rating: "thumbs_up",
                                    });
                                }}
                                style={{
                                    background: rating === "thumbs_up"
                                        ? CX.accent
                                        : "rgba(255,255,255,0.06)",
                                    color: rating === "thumbs_up"
                                        ? "white"
                                        : CX.textSecondary,
                                    border: "none",
                                    borderRadius: CX.radiusSm,
                                    padding: "6px 12px",
                                    cursor: "pointer",
                                    fontSize: 12,
                                    fontFamily: CX.font,
                                    fontWeight: 500,
                                }}
                            >Helpful</button>
                            <button
                                data-testid="rating-thumbs-down"
                                aria-label="Mark unhelpful"
                                aria-pressed={rating === "thumbs_down"}
                                onClick={() => {
                                    if (!interventionId) return;
                                    setRating("thumbs_down");
                                    setRatingTextOpen(true);
                                    safeSendMessage({
                                        type: "USER_RATING",
                                        intervention_id: interventionId,
                                        rating: "thumbs_down",
                                    });
                                }}
                                style={{
                                    background: rating === "thumbs_down"
                                        ? "#E47A6E"
                                        : "rgba(255,255,255,0.06)",
                                    color: rating === "thumbs_down"
                                        ? "white"
                                        : CX.textSecondary,
                                    border: "none",
                                    borderRadius: CX.radiusSm,
                                    padding: "6px 12px",
                                    cursor: "pointer",
                                    fontSize: 12,
                                    fontFamily: CX.font,
                                    fontWeight: 500,
                                }}
                            >Not helpful</button>
                        </div>
                    )}

                    {ratingTextOpen && (
                        <input
                            data-testid="rating-text-input"
                            type="text"
                            maxLength={200}
                            placeholder="What would have helped? (Enter to send, Esc to skip)"
                            value={ratingText}
                            onChange={(e) => setRatingText(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                    if (interventionId && ratingText.trim()) {
                                        safeSendMessage({
                                            type: "USER_RATING",
                                            intervention_id: interventionId,
                                            rating: "thumbs_down",
                                            context: ratingText.trim().slice(0, 200),
                                        });
                                    }
                                    setRatingText("");
                                    setRatingTextOpen(false);
                                } else if (e.key === "Escape") {
                                    setRatingText("");
                                    setRatingTextOpen(false);
                                }
                            }}
                            style={{
                                marginTop: 8,
                                width: "100%",
                                padding: "6px 10px",
                                fontSize: 11,
                                background: "rgba(255,255,255,0.04)",
                                color: CX.text,
                                border: `1px solid ${CX.accent}55`,
                                borderRadius: CX.radiusSm,
                                fontFamily: CX.font,
                                boxSizing: "border-box",
                            }}
                        />
                    )}
                </div>
            )}

            {/* Biometrics row — no card, 1px separators above/below */}
            {connected && hr ? (
                <div style={S.bioRow}>
                    <div style={S.bioCol}>
                        <span style={{ ...S.bioLabel, color: `${CX.bioHr}80` }}>BPM</span>
                        <span style={S.bioVal} aria-label={`${Math.round(hr)} beats per minute`}>{Math.round(hr)}</span>
                    </div>
                    <div style={S.bioCol}>
                        <span style={{ ...S.bioLabel, color: `${CX.bioHrv}80` }}>HRV</span>
                        <span style={S.bioVal} aria-label={hrv ? `${Math.round(hrv)} milliseconds heart rate variability` : "no HRV data"}>{hrv ? `${Math.round(hrv)}ms` : "\u2014"}</span>
                    </div>
                    <div style={S.bioCol}>
                        <span style={{ ...S.bioLabel, color: `${CX.bioBlink}80` }}>BLK</span>
                        <span style={S.bioVal} aria-label={blink ? `${Math.round(blink)} blinks per minute` : "no blink rate data"}>{blink ? `${Math.round(blink)}/m` : "\u2014"}</span>
                    </div>
                </div>
            ) : connected ? (
                <div
                    style={S.bioStatusBox}
                    role="status"
                    aria-live="polite"
                    aria-label={`Biometrics status: ${bioStatusMessage}`}
                >
                    {bioStatusMessage}
                </div>
            ) : null}

            {/* Settings — no card, just label + toggle, 1px separator above */}
            <div style={S.settingsArea}>
                <div style={S.toggleRow}>
                    <span style={S.toggleLabel}>Tab closing</span>
                    <button
                        style={{
                            ...S.toggleTrack,
                            background: tabCloseDisabled ? "rgba(255, 255, 255, 0.04)" : CX.accent,
                        }}
                        onClick={handleTabCloseToggle}
                        aria-label={tabCloseDisabled ? "Enable tab closing" : "Disable tab closing"}
                    >
                        <div style={{
                            ...S.toggleThumb,
                            transform: tabCloseDisabled ? "translateX(0)" : "translateX(16px)",
                        }} />
                    </button>
                </div>

                <div style={{ ...S.toggleRow, marginTop: 12 }}>
                    <span style={S.toggleLabel}>Quiet mode</span>
                    <button
                        style={{
                            ...S.toggleTrack,
                            background: quietMode ? CX.accent : "rgba(255, 255, 255, 0.04)",
                        }}
                        onClick={handleQuietModeToggle}
                        aria-label={quietMode ? "Disable quiet mode" : "Enable quiet mode"}
                    >
                        <div style={{
                            ...S.toggleThumb,
                            transform: quietMode ? "translateX(16px)" : "translateX(0)",
                        }} />
                    </button>
                </div>

                {/* P0 §3.11: kind selector + active-mode pill. The
                    daemon's QUIET_MODE_STATE broadcast is the source
                    of truth; clicking any pill emits the canonical
                    QUIET_MODE_TOGGLE wire frame. */}
                <div
                    style={{
                        display: "flex",
                        gap: 6,
                        marginTop: 8,
                        flexWrap: "wrap",
                    }}
                    role="group"
                    aria-label="Quiet mode kind"
                >
                    {([
                        { kind: "snooze_15", label: "Snooze 15m" },
                        { kind: "quiet_session", label: "Quiet" },
                        { kind: "pause", label: "Pause" },
                    ] as const).map((opt) => {
                        const active = quietModeKind === opt.kind;
                        return (
                            <button
                                key={opt.kind}
                                onClick={() => handleQuietModeKind(opt.kind)}
                                aria-pressed={active}
                                style={{
                                    flex: 1,
                                    padding: "6px 10px",
                                    fontSize: 11,
                                    fontFamily: CX.font,
                                    fontWeight: 500,
                                    color: active ? CX.accent : "rgba(255,255,255,0.72)",
                                    background: active
                                        ? "rgba(217,119,87,0.16)"
                                        : "rgba(255,255,255,0.04)",
                                    border: active
                                        ? `1px solid ${CX.accent}`
                                        : "1px solid rgba(255,255,255,0.06)",
                                    borderRadius: CX.radiusMd,
                                    cursor: "pointer",
                                    transition: `background ${CX.durationFast} ${CX.easeDefault}`,
                                }}
                            >
                                {opt.label}
                            </button>
                        );
                    })}
                </div>
                {quietModeKind !== "off" && (
                    <div
                        role="status"
                        style={{
                            marginTop: 8,
                            fontSize: 11,
                            color: CX.accent,
                            fontFamily: CX.font,
                        }}
                    >
                        {quietModeKind === "snooze_15" && "Snoozed"}
                        {quietModeKind === "quiet_session" && "Quiet for session"}
                        {quietModeKind === "pause" && "Paused"}
                        {quietModeDurationMin !== null && quietModeDurationMin > 0 && (
                            <span> · {quietModeDurationMin}m remaining</span>
                        )}
                        <button
                            onClick={() => handleQuietModeKind("off")}
                            style={{
                                marginLeft: 10,
                                background: "transparent",
                                border: "none",
                                color: CX.accent,
                                textDecoration: "underline",
                                cursor: "pointer",
                                fontSize: 11,
                                fontFamily: CX.font,
                                padding: 0,
                            }}
                            aria-label="Turn off quiet mode"
                        >
                            turn off
                        </button>
                    </div>
                )}

                <button
                    style={{
                        width: "100%",
                        marginTop: 16,
                        padding: "10px 0",
                        border: `1px solid ${CX.dangerDim}`,
                        borderRadius: CX.radiusMd,
                        background: CX.dangerDim,
                        color: CX.danger,
                        fontSize: 12,
                        fontWeight: 500,
                        fontFamily: CX.font,
                        cursor: stopping ? "default" : "pointer",
                        opacity: stopping ? 0.5 : 1,
                        transition: `opacity ${CX.durationFast} ${CX.easeDefault}`,
                    }}
                    onClick={handleStopCortex}
                    disabled={stopping}
                >
                    {stopping ? "Stopping\u2026" : "Stop Cortex"}
                </button>
            </div>

            {/* Today footer — no card, lowest hierarchy */}
            {dailyStats && (
                <div style={S.todayFooter}>
                    <div style={S.todayCol}>
                        <span style={S.todayVal}>{Math.round(dailyStats.totalFocusMin)}m</span>
                        <span style={S.todayLabel}>FOCUS</span>
                    </div>
                    <div style={S.todayCol}>
                        <span style={S.todayVal}>{dailyStats.sessions}</span>
                        <span style={S.todayLabel}>SESSIONS</span>
                    </div>
                    <div style={S.todayCol}>
                        <span style={S.todayVal}>{Math.round(dailyStats.longestStreakMin)}m</span>
                        <span style={S.todayLabel}>BEST</span>
                    </div>
                    <div style={S.todayCol}>
                        <span style={S.todayVal}>{dailyStats.distractionsBlocked}</span>
                        <span style={S.todayLabel}>BLOCKED</span>
                    </div>
                </div>
            )}

            {/* P0 §3.2: "Last 7 days" sparkbar mini-row. Sits between
                the Today footer (today's numbers) and the live state
                pill region (View history link below it), extending the
                at-a-glance summary into the past week without
                cluttering the real-time area. */}
            <TrendsMiniStrip />

            {/* P0 §3.1 / §3.3: View history footer. Terracotta-accented
                link that routes through the background script to raise
                the desktop dashboard's History tab. ``historyStatus``
                renders a one-line install hint if the native host is
                unavailable. */}
            <div style={S.historyFooter}>
                <button
                    style={S.historyLink}
                    onClick={handleOpenDashboardHistory}
                    data-testid="view-history-link"
                    aria-label="Open History tab in desktop dashboard"
                >View history <span aria-hidden="true">{"→"}</span></button>
                <span
                    aria-hidden="true"
                    style={{
                        color: CX.textTertiary,
                        margin: "0 8px",
                        fontSize: 11,
                    }}
                >·</span>
                {/* Phase 4d Task H / §3.24: in-app bug report. */}
                <button
                    style={S.historyLink}
                    onClick={() => setBugReportOpen(true)}
                    data-testid="report-bug-link"
                    aria-label="Report a bug"
                >Report bug</button>
                {historyStatus !== "" && (
                    <div
                        style={S.historyStatusLine}
                        data-testid="view-history-status"
                    >{historyStatus}</div>
                )}
                {/* F21 (Phase-4 audit / §3.15): BYOK spend indicator.
                    Hidden when provider is "none" or "rule_based"
                    (no LLM spend to report). Budget cap of 0 means
                    unlimited so we elide the "/ $X.XX" tail. */}
                {costInfo
                    && costInfo.provider !== null
                    && costInfo.provider !== "none"
                    && costInfo.provider !== "rule_based" && (
                        <div
                            data-testid="cost-indicator"
                            style={{
                                ...S.historyStatusLine,
                                color: costInfo.budget_exhausted
                                    ? CX.danger
                                    : CX.textTertiary,
                                marginTop: 4,
                            }}
                            aria-label={
                                costInfo.budget_today > 0
                                    ? `LLM spend today: $${costInfo.cost_today.toFixed(2)} of $${costInfo.budget_today.toFixed(2)} budget`
                                    : `LLM spend today: $${costInfo.cost_today.toFixed(2)}`
                            }
                        >
                            {`$${costInfo.cost_today.toFixed(2)}`}
                            {costInfo.budget_today > 0
                                ? ` / $${costInfo.budget_today.toFixed(2)} today`
                                : " today"}
                            {costInfo.budget_exhausted && " · budget hit"}
                        </div>
                    )}
                {bugReportStatus === "saved" && (
                    <div
                        role="status"
                        data-testid="bug-report-success"
                        style={{
                            ...S.historyStatusLine,
                            color: CX.accent,
                        }}
                    >Thanks — report sent.</div>
                )}
                {bugReportStatus === "queued" && (
                    <div
                        role="status"
                        data-testid="bug-report-queued"
                        style={S.historyStatusLine}
                    >Saved locally — will retry.</div>
                )}
            </div>

            {/* Phase 4d Task H / §3.24: bug report modal. */}
            {bugReportOpen && (
                <div
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="cortex-bug-report-title"
                    data-testid="bug-report-modal"
                    style={{
                        position: "fixed",
                        inset: 0,
                        background: "rgba(12,12,14,0.78)",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        zIndex: 100,
                    }}
                    onClick={(e) => {
                        if (e.target === e.currentTarget) {
                            setBugReportOpen(false);
                            setBugReportStatus("idle");
                            setBugReportError("");
                        }
                    }}
                >
                    <div
                        style={{
                            background: CX.surface,
                            border: `1px solid ${CX.borderDefault}`,
                            borderRadius: CX.radiusMd,
                            padding: 20,
                            width: 320,
                            boxShadow: CX.shadowFloat,
                        }}
                    >
                        <h2
                            id="cortex-bug-report-title"
                            style={{
                                fontSize: 15,
                                fontWeight: 600,
                                margin: "0 0 12px 0",
                                color: CX.text,
                                fontFamily: CX.fontSerif,
                            }}
                        >Report a bug</h2>
                        <textarea
                            data-testid="bug-report-textarea"
                            value={bugReportText}
                            onChange={(e) => setBugReportText(e.target.value)}
                            placeholder="What happened? (10-500 chars)"
                            maxLength={500}
                            rows={5}
                            aria-label="Bug description"
                            style={{
                                width: "100%",
                                background: "rgba(255,255,255,0.04)",
                                color: CX.text,
                                border: `1px solid ${CX.borderDefault}`,
                                borderRadius: CX.radiusSm,
                                padding: 10,
                                fontSize: 12,
                                fontFamily: CX.font,
                                resize: "vertical",
                                boxSizing: "border-box",
                                marginBottom: 10,
                            }}
                        />
                        <label
                            style={{
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                fontSize: 12,
                                color: CX.textSecondary,
                                fontFamily: CX.font,
                                marginBottom: 12,
                                cursor: "pointer",
                            }}
                        >
                            <input
                                type="checkbox"
                                data-testid="bug-report-logs-checkbox"
                                checked={bugReportIncludeLogs}
                                onChange={(e) =>
                                    setBugReportIncludeLogs(e.target.checked)
                                }
                            />
                            Include recent logs
                        </label>
                        {bugReportError !== "" && (
                            <div
                                role="alert"
                                data-testid="bug-report-error"
                                style={{
                                    color: "#E47A6E",
                                    fontSize: 11,
                                    marginBottom: 10,
                                    fontFamily: CX.font,
                                }}
                            >{bugReportError}</div>
                        )}
                        <div
                            style={{
                                display: "flex",
                                gap: 8,
                                justifyContent: "flex-end",
                            }}
                        >
                            <button
                                onClick={() => {
                                    setBugReportOpen(false);
                                    setBugReportStatus("idle");
                                    setBugReportError("");
                                }}
                                style={{
                                    padding: "6px 12px",
                                    background: "transparent",
                                    border: `1px solid ${CX.borderDefault}`,
                                    borderRadius: CX.radiusSm,
                                    color: CX.textSecondary,
                                    fontSize: 12,
                                    cursor: "pointer",
                                    fontFamily: CX.font,
                                }}
                            >Cancel</button>
                            <button
                                data-testid="bug-report-submit"
                                disabled={bugReportStatus === "submitting"}
                                onClick={handleBugReportSubmit}
                                style={{
                                    padding: "6px 14px",
                                    background: CX.accent,
                                    border: "none",
                                    borderRadius: CX.radiusSm,
                                    color: "white",
                                    fontSize: 12,
                                    fontWeight: 600,
                                    cursor: bugReportStatus === "submitting"
                                        ? "default"
                                        : "pointer",
                                    opacity: bugReportStatus === "submitting"
                                        ? 0.6
                                        : 1,
                                    fontFamily: CX.font,
                                }}
                            >
                                {bugReportStatus === "submitting"
                                    ? "Sending…"
                                    : "Submit"}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

// --- Styles ---

const S: Record<string, React.CSSProperties> = {
    root: {
        width: 380,
        maxHeight: 560,
        overflowY: "auto",
        padding: "20px",
        fontFamily: CX.font,
        fontSize: 14,
        color: CX.text,
        background: CX.surface,
        display: "flex",
        flexDirection: "column" as const,
    },

    // Alert toast
    alertBox: {
        padding: "16px",
        borderRadius: CX.radiusMd,
        background: CX.surface,
        border: `1px solid ${CX.borderDefault}`,
        boxShadow: CX.shadowFloat,
        marginBottom: 16,
        animation: "cxAlertIn 0.3s cubic-bezier(0.16, 1, 0.3, 1)",
    },
    alertTitle: { fontSize: 14, fontWeight: 600, marginBottom: 4, color: CX.text, fontFamily: CX.fontSerif },
    alertBody: { fontSize: 13, color: CX.textSecondary, lineHeight: 1.5 },

    // Header 
    header: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        height: 48,
        padding: "0 4px",
        marginBottom: 16,
    },
    logoText: {
        fontSize: 20,
        fontWeight: 600,
        letterSpacing: "-0.02em",
        color: CX.text,
        fontFamily: CX.fontSerif,
    },
    connectBtn: {
        padding: "6px 14px",
        border: "none",
        borderRadius: CX.radiusFull,
        background: CX.text,
        color: CX.textInverse,
        cursor: "pointer",
        fontSize: 13,
        fontWeight: 500,
        fontFamily: CX.font,
        transition: `background ${CX.durationNormal} ${CX.easeDefault}`,
    },
    statusRow: { display: "flex", alignItems: "center", gap: 8, paddingRight: 4 },
    statusLabel: {
        fontSize: 12,
        fontWeight: 500,
        fontFamily: CX.font,
        color: CX.textSecondary,
        transition: `color ${CX.durationSlow} ${CX.easeDefault}`,
    },

    // Disconnected area
    disconnectedArea: {
        display: "flex",
        flexDirection: "column" as const,
        alignItems: "center",
        justifyContent: "center",
        padding: "40px 0",
        gap: 8,
    },
    disconnectedTitle: { fontSize: 18, fontWeight: 600, color: CX.text, fontFamily: CX.fontSerif },
    disconnectedBody: { fontSize: 14, color: CX.textSecondary },

    // Morning briefing
    briefingCard: {
        background: CX.tertiary,
        borderRadius: CX.radiusMd,
        padding: 16,
        marginBottom: 16,
    },
    briefingTitle: { fontSize: 16, fontWeight: 600, color: CX.text, fontFamily: CX.fontSerif, marginBottom: 4 },
    briefingBody: { fontSize: 14, color: CX.textSecondary, lineHeight: 1.5 },

    // Ghost button
    ghostBtn: {
        padding: "8px 16px",
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusFull,
        background: "transparent",
        color: CX.text,
        cursor: "pointer",
        fontSize: 13,
        fontWeight: 500,
        fontFamily: CX.font,
    },

    // Goal input
    goalInput: {
        width: "100%",
        height: 44,
        padding: "0 40px 0 16px",
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusFull,
        background: CX.surface,
        color: CX.text,
        fontSize: 14,
        outline: "none",
        boxSizing: "border-box" as const,
        fontFamily: CX.font,
        marginBottom: 16,
        transition: "border-color 0.2s",
    },
    goalEnterIcon: {
        position: "absolute" as const,
        right: 16,
        top: 13,
        color: CX.textTertiary,
        fontSize: 16,
        pointerEvents: "none" as const,
    },

    // Session card 
    sessionCard: {
        background: CX.surface,
        borderRadius: CX.radiusMd,
        padding: "20px",
        marginBottom: 16,
        border: `1px solid ${CX.borderDefault}`,
        boxShadow: CX.shadowFloat,
    },
    focusHeader: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 20,
    },
    focusTitle: { fontSize: 18, fontWeight: 600, color: CX.text, fontFamily: CX.fontSerif },
    focusDuration: { fontSize: 13, color: CX.textSecondary },
    endBtn: {
        padding: "6px 14px",
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusFull,
        background: "transparent",
        color: CX.text,
        cursor: "pointer",
        fontSize: 12,
        fontWeight: 500,
        fontFamily: CX.font,
    },
    bigRow: {
        display: "flex",
        alignItems: "baseline",
        justifyContent: "space-between",
        marginBottom: 4,
    },
    bigNum: {
        fontSize: 36,
        fontWeight: 500,
        lineHeight: 1.15,
        fontFamily: CX.fontSerif,
    },
    bigPct: {
        fontSize: 15,
        fontWeight: 500,
        color: CX.textTertiary,
    },
    bigLabel: {
        fontSize: 13,
        color: CX.textSecondary,
        marginBottom: 20,
    },

    // Progress track
    trackOuter: {
        height: 4,
        borderRadius: CX.radiusFull,
        background: CX.tertiary,
        marginBottom: 20,
        overflow: "hidden",
    },
    trackFill: {
        height: "100%",
        borderRadius: CX.radiusFull,
        transition: `width 1s ease`,
    },

    // Stats row
    statsRow: { display: "flex", justifyContent: "space-between" },
    statCol: { display: "flex", flexDirection: "column" as const, alignItems: "flex-start", gap: 4 },
    statVal: { fontSize: 15, fontWeight: 600, color: CX.text },
    statLabel: { fontSize: 11, fontWeight: 500, color: CX.textTertiary, textTransform: "uppercase" },

    // Intervention preview
    interventionCard: {
        background: CX.surface,
        borderRadius: CX.radiusMd,
        padding: 20,
        marginBottom: 16,
        border: `1px solid ${CX.borderDefault}`,
        boxShadow: CX.shadowFloat,
    },
    causalText: {
        fontSize: 15,
        color: CX.textSecondary,
        lineHeight: 1.5,
        marginBottom: 16,
        fontFamily: CX.fontSerif,
        fontStyle: "italic",
    },
    tabRow: { display: "flex", alignItems: "center", gap: 12, height: 32 },
    tabXMark: { color: CX.danger, fontSize: 16, fontWeight: 500, width: 16, textAlign: "center" as const, flexShrink: 0 },
    tabName: {
        fontSize: 14, color: CX.text,
        whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis",
    },
    keepLine: { fontSize: 13, color: CX.textTertiary, marginTop: 8 },

    // Error
    errBox: {
        padding: 16,
        background: CX.tertiary,
        borderRadius: CX.radiusSm,
        marginBottom: 16,
    },
    errBody: { fontSize: 13, color: CX.text, lineHeight: 1.5 },
    errCode: {
        fontSize: 13, color: CX.accent, marginTop: 12, fontFamily: CX.mono,
        lineHeight: 1.5, whiteSpace: "pre-wrap" as const, margin: 0,
    },

    // Primary CTA
    primaryBtn: {
        width: "100%",
        height: 44,
        padding: "0 20px",
        border: "none",
        borderRadius: CX.radiusFull,
        background: CX.text,
        color: CX.textInverse,
        fontSize: 14,
        fontWeight: 500,
        cursor: "pointer",
        fontFamily: CX.font,
        transition: "opacity 0.2s ease",
    },
    doneBtnStyle: {
        background: STATE_COLORS.FLOW,
        color: CX.textInverse,
        cursor: "default",
        pointerEvents: "none" as const,
    },

    // Undo
    undoRow: {
        display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
        marginTop: 12, fontSize: 13, color: CX.textTertiary,
    },
    undoLink: {
        background: "none", border: "none", color: CX.text, fontSize: 13,
        fontWeight: 500, cursor: "pointer", padding: 0, fontFamily: CX.font,
        textDecoration: "underline",
    },

    // Biometrics row. ``minHeight`` is locked to the populated-state
    // height so swapping with ``bioStatusBox`` (rendered when no HR is
    // available) does not reflow the popup card.
    bioRow: {
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center" as const,
        padding: "16px 8px",
        marginBottom: 16,
        borderTop: `1px solid ${CX.borderDefault}`,
        borderBottom: `1px solid ${CX.borderDefault}`,
        boxSizing: "border-box" as const,
        minHeight: 74,
    },
    bioCol: { display: "flex", flexDirection: "column" as const, alignItems: "flex-start", gap: 4 },
    bioLabel: {
        fontSize: 11,
        fontWeight: 500,
        color: CX.textTertiary,
        textTransform: "uppercase" as const,
    },
    bioVal: {
        fontSize: 18,
        fontWeight: 400,
        fontFamily: CX.fontSerif,
        color: CX.text,
    },
    // Contextual banner shown in the BPM/HRV/BLK slot when no HR
    // reading has landed yet. ``minHeight`` matches ``bioRow`` so the
    // layout does not reflow when the first reading arrives. Italic +
    // secondary color signals "not your data". ``display: flex`` +
    // centering keeps the message vertically aligned regardless of
    // whether the string wraps to two lines.
    bioStatusBox: {
        display: "flex",
        alignItems: "center" as const,
        justifyContent: "center" as const,
        padding: "16px 12px",
        marginBottom: 16,
        borderTop: `1px solid ${CX.borderDefault}`,
        borderBottom: `1px solid ${CX.borderDefault}`,
        boxSizing: "border-box" as const,
        minHeight: 74,
        textAlign: "center" as const,
        fontSize: 12,
        fontStyle: "italic" as const,
        color: CX.textSecondary,
        fontFamily: CX.font,
        lineHeight: 1.4,
    },

    // Settings
    settingsArea: {
        padding: "8px 4px",
    },
    toggleRow: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "8px 0",
    },
    toggleLabel: { fontSize: 14, color: CX.text },
    toggleTrack: {
        position: "relative" as const,
        width: 40,
        height: 24,
        // Pill — clamps to half-height. Was hard-coded 12; CX.radiusFull
        // keeps macOS-toggle proportions stable if dimensions change.
        borderRadius: CX.radiusFull,
        border: "none",
        cursor: "pointer",
        padding: 0,
        flexShrink: 0,
        transition: `background ${CX.durationNormal} ${CX.easeDefault}`,
    },
    toggleThumb: {
        position: "absolute" as const,
        top: 2,
        left: 2,
        width: 20,
        height: 20,
        borderRadius: "50%",
        background: CX.textInverse,
        transition: `transform ${CX.durationNormal} ${CX.easeDefault}`,
        boxShadow: "0 2px 4px rgba(0,0,0,0.1)",
    },

    // Today footer
    todayFooter: {
        display: "flex",
        justifyContent: "space-between",
        padding: "20px 8px 0 8px",
        borderTop: `1px solid ${CX.borderDefault}`,
        marginTop: 16,
    },
    todayCol: { display: "flex", flexDirection: "column" as const, alignItems: "flex-start", gap: 4 },
    todayVal: { fontSize: 16, fontFamily: CX.fontSerif, color: CX.text },
    todayLabel: { fontSize: 11, color: CX.textTertiary, textTransform: "uppercase" },

    // P0 §3.2: "Last 7 days" sparkbar mini-row.
    trendsStrip: {
        display: "flex",
        flexDirection: "column" as const,
        gap: 6,
        padding: "12px 8px 0 8px",
    },
    trendsHeader: {
        display: "flex",
        alignItems: "baseline",
        justifyContent: "space-between",
        gap: 8,
    },
    trendsTitle: {
        fontSize: 11,
        color: CX.textTertiary,
        textTransform: "uppercase" as const,
        letterSpacing: "0.04em",
        fontFamily: CX.font,
    },
    trendsAvg: {
        fontSize: 11,
        color: CX.textTertiary,
        fontFamily: CX.mono,
    },
    trendsBars: {
        display: "flex",
        alignItems: "flex-end" as const,
        gap: 4,
        height: 16,
    },
    trendsEmpty: {
        fontSize: 11,
        color: CX.textTertiary,
        fontFamily: CX.font,
        lineHeight: 1.4,
    },

    // P0 §3.3: end-of-session recap card. Terracotta left edge keeps
    // it visually distinct from the white intervention card; warm
    // surface + small shadow match the existing popup aesthetic.
    recapCard: {
        background: CX.surface,
        borderRadius: CX.radiusMd,
        padding: 16,
        marginBottom: 16,
        border: `1px solid ${CX.borderDefault}`,
        borderLeft: `3px solid ${CX.accent}`,
        boxShadow: CX.shadowFloat,
    },
    recapHeaderRow: {
        display: "flex",
        alignItems: "baseline",
        justifyContent: "space-between",
        gap: 8,
        marginBottom: 6,
    },
    recapHeadline: {
        fontSize: 15,
        fontWeight: 600,
        color: CX.text,
        fontFamily: CX.fontSerif,
        lineHeight: 1.3,
    },
    recapDismissIcon: {
        background: "none",
        border: "none",
        color: CX.textTertiary,
        cursor: "pointer",
        fontSize: 16,
        padding: 0,
        fontFamily: CX.font,
        lineHeight: 1,
        flexShrink: 0,
    },
    recapBody: {
        fontSize: 13,
        color: CX.textSecondary,
        lineHeight: 1.5,
    },
    recapStat: {
        fontSize: 12,
        color: CX.textTertiary,
        marginTop: 4,
        fontFamily: CX.mono,
    },
    recapButtonRow: {
        display: "flex",
        gap: 8,
        marginTop: 12,
    },
    recapPrimaryBtn: {
        flex: 1,
        height: 32,
        padding: "0 12px",
        border: `1px solid ${CX.accent}`,
        borderRadius: CX.radiusFull,
        background: CX.accent,
        color: CX.textInverse,
        fontSize: 12,
        fontWeight: 500,
        fontFamily: CX.font,
        cursor: "pointer",
    },
    recapGhostBtn: {
        flex: 1,
        height: 32,
        padding: "0 12px",
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusFull,
        background: "transparent",
        color: CX.textSecondary,
        fontSize: 12,
        fontWeight: 500,
        fontFamily: CX.font,
        cursor: "pointer",
    },

    // P0 §3.1 / §3.3: View history footer.
    historyFooter: {
        display: "flex",
        flexDirection: "column" as const,
        alignItems: "center",
        gap: 4,
        padding: "16px 8px 4px 8px",
    },
    historyLink: {
        background: "none",
        border: "none",
        color: CX.accent,
        cursor: "pointer",
        fontSize: 12,
        fontWeight: 500,
        fontFamily: CX.font,
        padding: 0,
    },
    historyStatusLine: {
        fontSize: 11,
        color: CX.textTertiary,
        fontFamily: CX.font,
        textAlign: "center" as const,
    },
};

export default CortexPopup;
