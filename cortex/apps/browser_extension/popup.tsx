/**
 * Cortex Chrome Extension — Popup UI
 *
 * Design: warm paper surfaces, terracotta accent, SF system stack with a
 * system serif for display copy, 4 pt grid, 11 px caption floor. Text labels
 * only — no emoji glyphs, no motivational copy. Sentence case everywhere.
 * Colours come from the generated design tokens so the popup follows the
 * macOS appearance.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import "./page-reset.css";
import {
    CX,
    CX_KEYFRAMES,
    STATE_COLORS,
    STATE_LABELS,
    STATE_TEXT_COLORS,
} from "./design-tokens";
import { newCorrelationId } from "./lib/correlation";
import { getLastRuntimeError } from "./lib/chrome-runtime";
import { verifiedPresentedActionIds } from "./lib/intervention-transaction";
import { DAEMON_HTTP_URL } from "./config";
import { getAuthToken } from "./lib/auth";
import {
    getSiteAccessState,
    requestSiteAccess,
    revokeSiteAccess,
} from "./lib/site-access";
import {
    classifyConnectivity,
    connectivityBlocksSession,
    connectivityViewModel,
    normaliseMicroSteps,
    parseExecutionMode,
    supportStateViewModel,
    LAUNCH_FAILED_STATUS,
    LAUNCH_PENDING_STATUS,
    type CortexState,
    type DailyStats,
    type ExecutionMode,
    type FocusSnapshot,
    type MicroStep,
} from "./lib/popup-view-model";
export { classifyConnectivity, normaliseMicroSteps } from "./lib/popup-view-model";
import { reduceApplyResults } from "./lib/apply-state";
import {
    isConnectivityDiagnosticPayload,
    isExecuteAllRecommendedResponse,
    isInterventionAppliedMessage,
    isPopupInboundMessage,
} from "./lib/extension-protocol";
import { cleanTabReason, hasRealErrorAnalysis } from "./lib/reason-phrases";
import { S } from "./popup/styles";
import { ConnectionPanel, VersionBanner } from "./popup/components/ConnectionPanel";
import {
    IDLE_APPLY_STATE,
    InterventionCard,
    type ApplyState,
    type CausalSignalView,
} from "./popup/components/InterventionCard";
import { QuietModeControl } from "./popup/components/QuietModeControl";
import { StopCortex } from "./popup/components/StopCortex";
import type {
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

/** Storage key for the "use the browser's default new tab" preference. */
export const NEWTAB_DISABLED_KEY = "cortex_newtab_disabled";

const CortexLogo = () => (
    <svg
        width="22"
        height="22"
        viewBox="0 0 64 64"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        style={{ flexShrink: 0, color: CX.text }}
        aria-hidden="true"
    >
        <path d="M 51.8 12.2 A 28 28 0 1 0 51.8 51.8" fill="none" stroke="currentColor" strokeWidth="6" strokeLinecap="round" />
        <path d="M 12 32 L 22 32 L 27 15 L 37 49 L 42 32 L 60 32" fill="none" stroke={CX.accent} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="60" cy="32" r="3" fill={CX.accent} />
    </svg>
);

// --- Types ---

interface MorningBriefing {
    summary: string;
    action_items: string[];
    left_off_at: string;
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

    // ``cx-pulse`` is defined by the generated CX_KEYFRAMES; the dot breathes
    // for steady activity, drifts slowly for quiet activity, and stays still
    // when support may help — the user is already overwhelmed.
    switch (stateStr) {
        case "FLOW":
            return { ...base, animation: `cx-pulse 3s ${CX.easeInOut} infinite` };
        case "HYPO":
            return { ...base, animation: `cx-pulse 4s ${CX.easeInOut} infinite` };
        case "HYPER":
            return base;
        default:
            return base;
    }
}

function toCausalSignalViews(raw: unknown): CausalSignalView[] {
    const signals = Array.isArray(raw) ? raw : [];
    return signals
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
        });
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
 */
function TrendsMiniStrip(): React.ReactElement {
    const [trends, setTrends] = useState<TrendsResponse | null>(null);
    const [loadFailed, setLoadFailed] = useState(false);

    useEffect(() => {
        try {
            chrome.runtime.sendMessage(
                { type: "GET_CACHED_TRENDS" },
                (raw: unknown) => {
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
                    setLoadFailed(false);
                    const resp = raw as
                        | { trends: TrendsResponse | null; timestamp: number | null }
                        | undefined;
                    if (resp?.trends) {
                        setTrends(resp.trends);
                    }
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
            if (CORTEX_DEBUG) {
                console.warn(
                    "[cortex.popup] GET_CACHED_TRENDS threw",
                    err,
                );
            }
            setLoadFailed(true);
        }
    }, []);

    const trendsListener = useCallback((msg: Record<string, unknown>) => {
        if (msg.type !== "TRENDS_READY") return;
        const payload = msg.payload as TrendsResponse | undefined;
        if (payload) {
            setLoadFailed(false);
            setTrends(payload);
        }
    }, []);

    useEffect(() => {
        chrome.runtime.onMessage.addListener(trendsListener);
        return () => chrome.runtime.onMessage.removeListener(trendsListener);
    }, [trendsListener]);

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

    const daily: DailyBaseline[] = (trends?.daily ?? []).slice(-7);
    const minutes = daily.map((d) => Math.max(0, Math.round(d.total_flow_minutes ?? 0)));
    const maxMin = minutes.reduce((m, v) => (v > m ? v : m), 0);
    const totalMin = minutes.reduce((s, v) => s + v, 0);
    const avgMin = daily.length > 0 ? Math.round(totalMin / daily.length) : 0;

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
                {loadFailed && (
                    <span data-testid="trends-stale-badge" style={S.trendsStale}>
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
    const [nativeHostError, setNativeHostError] = useState<string | null>(null);
    const [daemonVersion, setDaemonVersion] = useState<string | null>(null);
    const [handshakeError, setHandshakeError] = useState<string | null>(null);
    const [versionBannerDismissed, setVersionBannerDismissed] = useState(false);
    const [stopRequested, setStopRequested] = useState(false);
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
    const [presentedManifest, setPresentedManifest] = useState<{
        interventionId: string;
        status: "pending" | "verified" | "invalid";
        executableActionIds: string[];
    } | null>(null);
    const [tabRecs, setTabRecs] = useState<TabRecommendations | null>(null);
    const [errAnalysis, setErrAnalysis] = useState<Record<string, string> | null>(null);
    const [interventionId, setInterventionId] = useState<string>("");
    const [executionMode, setExecutionMode] = useState<ExecutionMode>(
        "suggest_only",
    );
    // One apply state machine (lib/apply-state.ts): idle → pending →
    // applied | partial | failed. The worker computes the outcome; the
    // popup only renders it.
    const [applyState, setApplyState] = useState<ApplyState>(IDLE_APPLY_STATE);
    const [interventionError, setInterventionError] = useState<string | null>(
        null,
    );
    const [interventionPrompt, setInterventionPrompt] = useState<string | null>(
        null,
    );
    const [causalExplanation, setCausalExplanation] = useState<string>("");
    const [microSteps, setMicroSteps] = useState<MicroStep[]>([]);
    const [briefing, setBriefing] = useState<MorningBriefing | null>(null);
    const [newtabDisabled, setNewtabDisabled] = useState(false);
    const [quietMode, setQuietMode] = useState(false);
    const [quietModeKind, setQuietModeKind] = useState<string>("off");
    const [quietModeEndsAt, setQuietModeEndsAt] = useState<number | null>(null);
    const [quietModeDurationMin, setQuietModeDurationMin] = useState<number | null>(null);
    const [siteAccess, setSiteAccess] = useState<
        "checking" | "granted" | "denied" | "unavailable" | "busy"
    >("checking");
    const [siteAccessOrigin, setSiteAccessOrigin] = useState<string | null>(null);
    const [siteAccessError, setSiteAccessError] = useState("");
    useEffect(() => {
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
        const handle = setInterval(tick, 30_000);
        return () => clearInterval(handle);
    }, [quietModeEndsAt, quietMode]);
    const [launching, setLaunching] = useState(false);
    const [launchError, setLaunchError] = useState(false);
    const [tabsExpanded, setTabsExpanded] = useState(false);
    const [rating, setRating] = useState<"thumbs_up" | "thumbs_down" | null>(null);
    const [ratingTextOpen, setRatingTextOpen] = useState<boolean>(false);
    const [ratingText, setRatingText] = useState<string>("");
    const [interventionLevel, setInterventionLevel] = useState<
        "overlay_only" | "simplified_workspace" | "guided_mode"
    >("overlay_only");
    const [causalSignals, setCausalSignals] = useState<CausalSignalView[]>([]);
    const [whyOpen, setWhyOpen] = useState<boolean>(false);
    const [whyError, setWhyError] = useState<string | null>(null);
    const [recap, setRecap] = useState<SessionReport | null>(null);
    const [recapTimestamp, setRecapTimestamp] = useState<number | null>(null);
    const [recapPersisted, setRecapPersisted] = useState<boolean | null>(null);
    const [historyStatus, setHistoryStatus] = useState<string>("");
    const [bugReportOpen, setBugReportOpen] = useState(false);
    const [bugReportText, setBugReportText] = useState("");
    const [bugReportIncludeLogs, setBugReportIncludeLogs] = useState(true);
    const [bugReportStatus, setBugReportStatus] = useState<
        "idle" | "submitting" | "saved" | "queued" | "error"
    >("idle");
    const [bugReportError, setBugReportError] = useState<string>("");
    const bugDialogRef = useRef<HTMLDivElement>(null);
    const bugTextareaRef = useRef<HTMLTextAreaElement>(null);

    const closeBugReport = useCallback(() => {
        setBugReportOpen(false);
        setBugReportStatus("idle");
        setBugReportError("");
    }, []);

    // Keep the privacy-sensitive report sheet keyboard-contained. Escape
    // dismisses, Tab wraps inside the sheet, and closing restores the control
    // that opened it instead of dropping focus at the document root.
    useEffect(() => {
        if (!bugReportOpen) return;
        const previousFocus = document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null;
        const focusFrame = window.requestAnimationFrame(() => {
            bugTextareaRef.current?.focus({ preventScroll: true });
        });
        const handleKeydown = (event: KeyboardEvent) => {
            if (event.key === "Escape") {
                event.preventDefault();
                closeBugReport();
                return;
            }
            if (event.key !== "Tab" || !bugDialogRef.current) return;
            const focusable = Array.from(
                bugDialogRef.current.querySelectorAll<HTMLElement>(
                    'button:not([disabled]), textarea:not([disabled]), input:not([disabled])',
                ),
            );
            if (focusable.length === 0) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };
        document.addEventListener("keydown", handleKeydown);
        return () => {
            window.cancelAnimationFrame(focusFrame);
            document.removeEventListener("keydown", handleKeydown);
            previousFocus?.focus({ preventScroll: true });
        };
    }, [bugReportOpen, closeBugReport]);

    // F21: poll the daemon's /api/cost endpoint every 30s while the popup
    // is open. The background script proxies the fetch.
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

    // Inject keyframes + interaction states (single injection point)
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
            button:not(:disabled) {
                transition: transform 120ms ${CX.easeOut},
                    background-color 120ms ${CX.easeOut},
                    border-color 120ms ${CX.easeOut},
                    color 120ms ${CX.easeOut},
                    opacity 120ms ${CX.easeOut},
                    box-shadow 120ms ${CX.easeOut} !important;
            }
            button:active:not(:disabled) {
                transform: scale(0.97);
            }
            button:focus-visible {
                outline: 2px solid ${CX.accent};
                outline-offset: 2px;
            }
            .cortex-primary-btn:active:not(:disabled) {
                box-shadow: inset 0 0 0 1px color-mix(in srgb, ${CX.textInverse} 24%, transparent);
                transform: scale(0.97);
            }
            .cortex-ghost-btn:active:not(:disabled) {
                background: ${CX.accentDim};
                transform: scale(0.97);
            }
            .cortex-progress-motion {
                transform-origin: left center;
            }
            @media (hover: hover) and (pointer: fine) {
                .cortex-primary-btn:hover:not(:disabled) {
                    box-shadow: inset 0 0 0 1px color-mix(in srgb, ${CX.textInverse} 16%, transparent);
                }
                .cortex-ghost-btn:hover:not(:disabled) {
                    background: ${CX.accentDim};
                    border-color: ${CX.borderEmphasis};
                }
            }
            @media (prefers-reduced-motion: reduce) {
                button:not(:disabled) {
                    transition-property: background-color, border-color, color, opacity !important;
                }
                button:active:not(:disabled) {
                    transform: none;
                }
                .cortex-progress-motion {
                    transition: none !important;
                }
            }
        `;
        document.head.appendChild(style);
        return () => { style.remove(); };
    }, []);

    useEffect(() => {
        let active = true;
        void getSiteAccessState().then((result) => {
            if (!active) return;
            setSiteAccessOrigin(result.origin);
            setSiteAccess(
                !result.available
                    ? "unavailable"
                    : result.granted
                    ? "granted"
                    : "denied",
            );
        });
        return () => { active = false; };
    }, []);

    const handleSiteAccess = useCallback(async () => {
        if (
            siteAccess === "busy"
            || siteAccess === "checking"
            || siteAccess === "unavailable"
            || !siteAccessOrigin
        ) return;
        const wasGranted = siteAccess === "granted";
        setSiteAccess("busy");
        setSiteAccessError("");
        try {
            const changed = wasGranted
                ? await revokeSiteAccess(siteAccessOrigin)
                : await requestSiteAccess(siteAccessOrigin);
            const current = await getSiteAccessState();
            setSiteAccessOrigin(current.origin);
            setSiteAccess(
                !current.available
                    ? "unavailable"
                    : current.granted
                    ? "granted"
                    : "denied",
            );
            if (!changed && current.granted === wasGranted) {
                setSiteAccessError(
                    wasGranted
                        ? "Chrome could not revoke site access."
                        : "Site access was not granted.",
                );
            }
            if (wasGranted && !current.granted) {
                safeSendMessage({ type: "SITE_ACCESS_REVOKED" });
            }
        } catch {
            setSiteAccess(wasGranted ? "granted" : "denied");
            setSiteAccessError("Chrome could not update site access.");
        }
    }, [siteAccess, siteAccessOrigin]);

    // Load preferences and the cached quiet-mode envelope on mount
    useEffect(() => {
        chrome.storage.local.get(NEWTAB_DISABLED_KEY, (result) => {
            if (result?.[NEWTAB_DISABLED_KEY] === true) setNewtabDisabled(true);
        });
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
                    if (typeof cached.ends_at_unix_ms === "number") {
                        setQuietModeEndsAt(cached.ends_at_unix_ms);
                    } else if (typeof cached.ends_at === "number") {
                        setQuietModeEndsAt((cached.ends_at as number) * 1000);
                    }
                    if (typeof cached.duration_minutes === "number") {
                        setQuietModeDurationMin(cached.duration_minutes as number);
                    }
                }
            },
        );
    }, []);

    const handleNewtabToggle = useCallback(() => {
        const next = !newtabDisabled;
        setNewtabDisabled(next);
        chrome.storage.local.set({ [NEWTAB_DISABLED_KEY]: next });
    }, [newtabDisabled]);

    const handleQuietModeKind = useCallback(
        (kind: "snooze_15" | "quiet_session" | "pause" | "off") => {
            // Optimistic local update — the daemon's QUIET_MODE_STATE
            // broadcast reconciles within a few hundred ms.
            setQuietModeKind(kind);
            setQuietMode(kind !== "off");
            if (kind === "off") setQuietModeEndsAt(null);
            sendWithCid({
                type: "QUIET_MODE_TOGGLE",
                kind,
                duration_minutes: kind === "snooze_15" ? 15 : null,
            });
        },
        [],
    );

    const [launchStatus, setLaunchStatus] = useState("");

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
        setLaunchStatus(LAUNCH_PENDING_STATUS);
        setStopRequested(false);
        sendWithCid({ type: "LAUNCH_CORTEX" }, (raw: unknown) => {
            const resp = raw as { ok?: boolean; status?: string } | undefined;
            setLaunching(false);
            if (resp?.ok && resp.status === "camera_enabled") {
                setLaunchStatus("");
            } else {
                setLaunchError(true);
                setLaunchStatus(LAUNCH_FAILED_STATUS);
                setTimeout(() => { setLaunchError(false); setLaunchStatus(""); }, 30000);
            }
        });
    }, []);

    const adoptIntervention = useCallback((p: Record<string, unknown>) => {
        setExecutionMode(parseExecutionMode(p.execution_mode));
        const rawActions = (p.suggested_actions as Record<string, unknown>[]) || [];
        const recs = (p.tab_recommendations as TabRecommendations | undefined) ?? null;
        setActiveActions(rawActions);
        setTabRecs(recs);
        setErrAnalysis((p.error_analysis as Record<string, string>) || null);
        const incomingInterventionId = String(p.intervention_id || "");
        setInterventionId(incomingInterventionId);
        setPresentedManifest({
            interventionId: incomingInterventionId,
            status: "pending",
            executableActionIds: [],
        });
        void verifiedPresentedActionIds(p, "browser")
            .then((ids) => setPresentedManifest({
                interventionId: incomingInterventionId,
                status: "verified",
                executableActionIds: ids,
            }))
            .catch(() => setPresentedManifest({
                interventionId: incomingInterventionId,
                status: "invalid",
                executableActionIds: [],
            }));
        setMicroSteps(normaliseMicroSteps(p.micro_steps));
        setApplyState(IDLE_APPLY_STATE);
    }, []);

    useEffect(() => {
        safeSendMessage({ type: "GET_STATE" }, (raw) => {
            const resp = raw as {
                connected: boolean;
                stopRequested?: boolean;
                state: CortexState | null;
                focusSession: FocusSnapshot | null;
                intervention?: Record<string, unknown>;
            } | undefined;
            if (!resp) return;
            setConnected(resp.connected);
            setStopRequested(resp.stopRequested === true);
            setState(resp.state);
            setFocus(resp.focusSession);
            if (resp.intervention) adoptIntervention(resp.intervention);
        });
        safeSendMessage({ type: "GET_DAILY_STATS" }, (raw) => {
            const stats = raw as DailyStats | undefined;
            if (stats) setDailyStats(stats);
        });
        safeSendMessage({ type: "REQUEST_CONNECTIVITY_DIAGNOSTIC" });
        safeSendMessage({ type: "GET_CACHED_RECAP" }, (raw) => {
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
            safeSendMessage({ type: "RECAP_VIEWED" });
        });
    }, [adoptIntervention]);

    const clearInterventionCard = useCallback(() => {
        setActiveActions([]);
        setPresentedManifest(null);
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
        setApplyState(IDLE_APPLY_STATE);
        setInterventionError(null);
        setInterventionPrompt(null);
    }, []);

    // F50: stable listener identity so addListener/removeListener refer
    // to the same function across re-renders.
    const popupMessageListener = useCallback((msg: Record<string, unknown>) => {
        if (isPopupInboundMessage(msg)) {
            switch (msg.type) {
                case "CONNECTION_CHANGED":
                    setConnected(msg.connected);
                    // A live connection is proof the handshake succeeded;
                    // never keep a stale rejection over a healthy socket.
                    if (msg.connected) {
                        setHandshakeError(null);
                        setStopRequested(false);
                    }
                    return;
                case "CONNECTIVITY_DIAGNOSTIC": {
                    const d = msg.payload;
                    if (d.native_host_status !== "unknown") {
                        setNativeHostStatus(d.native_host_status);
                    }
                    setNativeHostError(d.native_host_error);
                    setDaemonVersion(d.daemon_version);
                    setHandshakeError(d.handshake_error);
                    return;
                }
                case "INTERVENTION_APPLIED":
                    // The page panel (or another popup) applied this
                    // proposal; render the same outcome here.
                    setApplyState({
                        phase: msg.outcome.phase,
                        outcome: msg.outcome,
                        undone: false,
                        undoBusy: false,
                    });
                    return;
                case "INTERVENTION_RESTORE":
                case "OVERLAY_DISMISSED":
                    clearInterventionCard();
                    return;
                case "STOP_INTENT":
                    setStopRequested(msg.stopRequested);
                    return;
            }
        }
        switch (msg.type) {
            case "STATE_UPDATE":
                setState(msg.payload as CortexState);
                if (msg.focusSession) setFocus(msg.focusSession as FocusSnapshot);
                break;
            case "FOCUS_SESSION_STARTED": {
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
                adoptIntervention(p);
                setCausalExplanation(String(p.causal_explanation || ""));
                const level = String(p.level || "");
                setInterventionLevel(
                    level === "guided_mode" || level === "simplified_workspace"
                        ? level
                        : "overlay_only",
                );
                setCausalSignals(toCausalSignalViews(p.causal_signals));
                setRating(null);
                setRatingTextOpen(false);
                setRatingText("");
                setWhyOpen(false);
                setWhyError(null);
                setInterventionError(null);
                setInterventionPrompt(null);
                break;
            }
            case "INTERVENTION_FAILED": {
                // The daemon's executor returned only failed mutations — the
                // workspace was NOT changed. Show the reason and settle the
                // apply control in its failed state; never "Done".
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
                setApplyState({
                    phase: "failed",
                    outcome: reduceApplyResults({ success: false, message: body }),
                    undone: false,
                    undoBusy: false,
                });
                break;
            }
            case "INTERVENTION_PROMPT": {
                const p = msg.payload as Record<string, unknown>;
                const prompt = String(p.prompt || "").trim();
                setInterventionPrompt(prompt.length > 0 ? prompt : null);
                break;
            }
            case "BREAK_RECOMMENDATION": {
                // Compatibility-only message. Never render an unsupported
                // HRV/stress claim, even if an older daemon emits one.
                break;
            }
            case "WHY_DETAIL": {
                const p = msg.payload as Partial<WhyDetailSchema> & Record<string, unknown>;
                setCausalSignals(toCausalSignalViews(p.causal_signals));
                const errVal = (p as { error?: unknown }).error;
                setWhyError(typeof errVal === "string" && errVal.length > 0 ? errVal : null);
                setWhyOpen(true);
                break;
            }
            case "SETTINGS_SYNC": {
                const settings = msg.payload as Record<string, unknown>;
                setExecutionMode(parseExecutionMode(settings.execution_mode));
                if (typeof settings.quiet_mode === "boolean") {
                    setQuietMode(settings.quiet_mode);
                }
                break;
            }
            case "QUIET_MODE_STATE": {
                const quiet = msg.payload as Record<string, unknown>;
                const kind = typeof quiet.kind === "string" ? quiet.kind : "off";
                setQuietModeKind(kind);
                setQuietMode(kind !== "off");
                setQuietModeEndsAt(
                    typeof quiet.ends_at_unix_ms === "number"
                        ? quiet.ends_at_unix_ms
                        : typeof quiet.ends_at === "number"
                            ? (quiet.ends_at as number) * 1000
                            : null,
                );
                setQuietModeDurationMin(
                    typeof quiet.duration_minutes === "number"
                        ? (quiet.duration_minutes as number)
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
                // Partial diagnostics (older workers) still update what
                // they carry.
                const d = msg.payload as Record<string, unknown> | undefined;
                if (!d || isConnectivityDiagnosticPayload(d)) break;
                if (d.native_host_status === "present" || d.native_host_status === "missing") {
                    setNativeHostStatus(d.native_host_status);
                }
                if (typeof d.native_host_error === "string") setNativeHostError(d.native_host_error.slice(0, 240));
                if (d.native_host_error === null) setNativeHostError(null);
                if (typeof d.daemon_version === "string") setDaemonVersion(d.daemon_version);
                if (d.daemon_version === null) setDaemonVersion(null);
                if (typeof d.handshake_error === "string") setHandshakeError(d.handshake_error);
                if (d.handshake_error === null) setHandshakeError(null);
                break;
            }
            case "SESSION_RECAP_READY": {
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
    }, [adoptIntervention, clearInterventionCard]);

    useEffect(() => {
        chrome.runtime.onMessage.addListener(popupMessageListener);
        return () => chrome.runtime.onMessage.removeListener(popupMessageListener);
    }, [popupMessageListener]);

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
                    setTimeout(() => setHistoryStatus(""), 8000);
                } else {
                    setHistoryStatus("");
                }
            },
        );
    }, []);

    const handleDismissRecap = useCallback(() => {
        safeSendMessage({ type: "DISMISS_RECAP" });
        setRecap(null);
        setRecapTimestamp(null);
    }, []);

    const handleBugReportSubmit = useCallback(async () => {
        const description = bugReportText.trim();
        if (description.length < 10 || description.length > 500) {
            setBugReportError("Description must be 10-500 characters.");
            return;
        }
        setBugReportStatus("submitting");
        setBugReportError("");
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

    // Drain the ``pending_feedback`` queue on mount.
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

    // Auto-dismiss the recap when its 24h TTL crosses while the popup is open.
    useEffect(() => {
        if (recap == null || recapTimestamp == null) return;
        const elapsed = Date.now() - recapTimestamp;
        const remaining = RECAP_TTL_MS - elapsed;
        if (remaining <= 0) {
            handleDismissRecap();
            return;
        }
        const handle = setTimeout(handleDismissRecap, remaining);
        return () => clearTimeout(handle);
    }, [recap, recapTimestamp, handleDismissRecap]);

    const [stopping, setStopping] = useState(false);
    const handleStopCortex = useCallback(() => {
        setStopping(true);
        // Force local UI to disconnected immediately
        setConnected(false);
        setState(null);
        setFocus(null);
        sendWithCid({ type: "STOP_CORTEX" });
        setTimeout(() => {
            setStopping(false);
            setStopRequested(true);
        }, 2000);
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

    // Derived presentation models keep transport semantics out of the JSX.
    const supportView = supportStateViewModel(state, connected, STATE_LABELS);
    const estimateReady = state?.status === undefined || state.status === "estimated";
    const stateStr = supportView.stateKey;
    const stateColor = STATE_COLORS[stateStr] || CX.textTertiary;
    const stateTextColor = STATE_TEXT_COLORS[stateStr] || CX.textSecondary;
    const stateLabel = supportView.label;
    const hr = state?.biometrics?.heart_rate;
    const blink = state?.biometrics?.blink_rate;
    const captureStale = supportView.captureStale;
    const storeDegraded = supportView.storeDegraded;
    const bioStatusMessage = supportView.bioStatusMessage;

    const focusMin = focus ? Math.round(focus.focusMs / 60000) : 0;
    const elapsedMin = focus ? Math.round(focus.elapsedMs / 60000) : 0;
    const streakSec = focus ? Math.round(focus.currentStreakMs / 1000) : 0;
    const streakMin = Math.floor(streakSec / 60);
    const streakRemSec = streakSec % 60;

    const closeTabs = tabRecs?.tabs?.filter(t => t.action === "close" || t.action === "bookmark_and_close") || [];
    const keepTabs = tabRecs?.tabs?.filter(t => t.action === "keep") || [];
    const rec = activeActions.filter(a => a.category === "recommended");
    const manifestForCurrent = presentedManifest?.interventionId === interventionId
        ? presentedManifest
        : null;
    const executableIdSet = new Set(
        manifestForCurrent?.status === "verified"
            ? manifestForCurrent.executableActionIds
            : [],
    );
    const executableRec = rec.filter((action) =>
        typeof action.action_id === "string"
        && executableIdSet.has(action.action_id)
    );
    const manualRec = rec.filter((action) =>
        typeof action.action_id !== "string"
        || !executableIdSet.has(action.action_id)
    );
    const canExecutePresented = executionMode !== "suggest_only"
        && manifestForCurrent?.status === "verified"
        && executableRec.length > 0;

    const visibleCloseTabs = (tabsExpanded ? closeTabs : closeTabs.slice(0, 5)).map((t) => ({
        title: String(t.tab_title || "Untitled"),
        reason: cleanTabReason(t.reason),
    }));
    const overflowCount = tabsExpanded ? 0 : closeTabs.length - visibleCloseTabs.length;

    const realErrAnalysis = errAnalysis && hasRealErrorAnalysis(errAnalysis.root_cause)
        ? {
            rootCause: errAnalysis.root_cause,
            suggestedFix: errAnalysis.suggested_fix ?? "",
        }
        : null;

    const realCausal = causalExplanation && causalExplanation.length > 20
        && /\d/.test(causalExplanation) ? causalExplanation : "";

    const hasIntervention =
        activeActions.length > 0 ||
        tabRecs ||
        realErrAnalysis ||
        microSteps.length > 0 ||
        interventionError !== null ||
        interventionPrompt !== null;

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

    const handleApply = useCallback(() => {
        if (!canExecutePresented || applyState.phase !== "idle") return;
        setApplyState({ phase: "pending", outcome: null, undone: false, undoBusy: false });
        sendWithCid(
            {
                type: "EXECUTE_ALL_RECOMMENDED",
                intervention_id: interventionId,
            },
            (raw: unknown) => {
                const outcome = isExecuteAllRecommendedResponse(raw)
                    ? raw.outcome
                    : reduceApplyResults(raw, "Cortex didn't respond");
                setApplyState({
                    phase: outcome.phase,
                    outcome,
                    undone: false,
                    undoBusy: false,
                });
            },
        );
    }, [canExecutePresented, applyState.phase, interventionId]);

    const handleUndo = useCallback(() => {
        setApplyState((prev) => ({ ...prev, undoBusy: true }));
        sendWithCid(
            { type: "UNDO_ALL_RECENT", intervention_id: interventionId },
            () => setApplyState({
                phase: "idle",
                outcome: null,
                undone: true,
                undoBusy: false,
            }),
        );
    }, [interventionId]);

    const handleRate = useCallback((value: "thumbs_up" | "thumbs_down") => {
        if (!interventionId) return;
        setRating(value);
        if (value === "thumbs_down") setRatingTextOpen(true);
        safeSendMessage({
            type: "USER_RATING",
            intervention_id: interventionId,
            rating: value,
        });
    }, [interventionId]);

    const handleRatingTextSubmit = useCallback(() => {
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
    }, [interventionId, ratingText]);

    const connectivityView = connectivityViewModel({
        state: connectivity,
        launching,
        launchError,
        launchStatus,
        expectedVersion: EXPECTED_VERSION,
        daemonVersion,
        handshakeError,
        nativeHostError,
    });
    const handleConnectivityAction = (): void => {
        if (connectivityView.action === "install") {
            void chrome.tabs.create({
                url: chrome.runtime.getURL("tabs/onboarding.html"),
            });
        } else if (connectivityView.action === "handshake") {
            sendWithCid({ type: "CONNECT" });
        } else {
            handleLaunchCortex();
        }
    };
    const sessionBlocked = connectivityBlocksSession(connectivity);
    const showVersionBanner = connectivity === "installed_version_mismatch"
        && !versionBannerDismissed;

    return (
        <div style={S.root}>
            {/* Alert toast — top-right, auto-dismiss 10s */}
            {alert && (
                <div className="cortex-motion-enter" style={S.alertBox}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                        <div style={S.alertTitle}>{alert.title}</div>
                        <button
                            aria-label="Dismiss health notification"
                            style={S.iconButton}
                            onClick={() => setAlert(null)}
                        >{"×"}</button>
                    </div>
                    <div style={S.alertBody}>{alert.body}</div>
                </div>
            )}

            {/* Header — logo + one status pill. The CTA lives in the panel. */}
            <div style={S.header}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <CortexLogo />
                    <span style={{ ...S.logoText, fontFamily: CX.fontBrand, fontStyle: "italic", letterSpacing: "0.02em" }}>Cortex.</span>
                </div>
                <div style={S.statusRow} aria-live="polite" data-testid="status-pill">
                    <div
                        aria-hidden="true"
                        className={connected ? "cortex-motion-ambient" : undefined}
                        style={getStateDotStyle(connected ? stateStr : "", connected ? stateColor : CX.textTertiary)}
                    />
                    <span
                        style={{ ...S.statusLabel, color: connected ? stateTextColor : CX.textSecondary }}
                        title={connected && estimateReady && state?.evidence_coverage !== undefined
                            ? `Evidence coverage ${Math.round(state.evidence_coverage * 100)}%`
                            : undefined}
                    >{stateLabel}</span>
                </div>
            </div>

            {/* Envelope-level health warning strip. */}
            {connected && (captureStale || storeDegraded) && (
                <div
                    role="status"
                    aria-live="polite"
                    data-testid="cortex-health-banner"
                    style={{
                        margin: "0 16px 8px",
                        padding: "8px 12px",
                        borderRadius: CX.radiusSm,
                        background: CX.dangerDim,
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

            {/* Version skew is a notice, never a wall: the session stays live. */}
            {showVersionBanner && (
                <VersionBanner
                    view={connectivityView}
                    onOpen={handleLaunchCortex}
                    onDismiss={() => setVersionBannerDismissed(true)}
                />
            )}

            {/* End-of-session recap card. */}
            {recap && (recapTimestamp == null || Date.now() - recapTimestamp <= RECAP_TTL_MS) && (() => {
                const r = recap;
                const durationMin = Math.round((r.duration_seconds ?? 0) / 60);
                const flowPct = Math.round(r.flow_percentage ?? 0);
                const breaks = r.breaks_taken ?? 0;
                const recapStreakMin = Math.round(
                    (r.longest_flow_streak_seconds ?? 0) / 60,
                );
                const avgHr = r.avg_hr_bpm;
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
                            <div
                                style={S.recapStat}
                                data-testid="recap-not-persisted"
                            >
                                Saving to history…
                            </div>
                        )}
                        {avgHr != null && (
                            <div style={S.recapStat}>
                                Avg HR {Math.round(avgHr)} bpm
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
                            aria-label="Dismiss briefing"
                            style={{ ...S.iconButton, marginLeft: 8 }}
                            onClick={() => setBriefing(null)}
                        >{"×"}</button>
                    </div>
                    <div style={{ marginTop: 8 }}>
                        <button className="cortex-ghost-btn" style={S.ghostBtn} onClick={() => {
                            const leftOff = (briefing.left_off_at ?? "").trim();
                            if (leftOff !== "") {
                                sendWithCid({ type: "START_FOCUS", goal: leftOff });
                            }
                            setBriefing(null);
                        }}>Resume</button>
                    </div>
                </div>
            )}

            {/* One connection panel for every blocking state. */}
            {sessionBlocked && (
                <ConnectionPanel
                    view={connectivityView}
                    launching={launching}
                    onAction={handleConnectivityAction}
                />
            )}

            {/* Goal input — one input, Enter to start, no separate button */}
            {connected && !focus && (
                <div style={{ marginBottom: CX.space6, position: "relative" as const }}>
                    <input
                        className="cortex-goal-input"
                        style={S.goalInput}
                        placeholder="What are you working on?"
                        aria-label="Focus goal"
                        value={goalInput}
                        autoFocus
                        maxLength={500}
                        onChange={(e) => setGoalInput(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && handleStartFocus()}
                    />
                    <span style={S.goalEnterIcon} aria-hidden="true">{"⏎"}</span>
                </div>
            )}

            {/* Active focus session — sticky */}
            {focus && (
                <div style={{ ...S.sessionCard, position: "sticky" as const, top: 0, zIndex: 10 }}>
                    <div style={S.focusHeader}>
                        <div style={{ display: "flex", alignItems: "baseline", gap: 6, minWidth: 0 }}>
                            <span style={S.focusTitle}>{focus.goal}</span>
                            <span style={S.focusDuration}>{"·"} {elapsedMin}m</span>
                        </div>
                        <button style={S.endBtn} onClick={handleStopFocus}>End</button>
                    </div>
                    {focus.autoArmed && (
                        <div
                            role="status"
                            style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 6,
                                marginTop: 8,
                                padding: "4px 12px",
                                fontSize: 11,
                                fontFamily: CX.font,
                                fontWeight: 500,
                                color: CX.accentText,
                                background: CX.accentDim,
                                border: `1px solid color-mix(in srgb, ${CX.accent} 36%, transparent)`,
                                borderRadius: CX.radiusMd,
                                width: "fit-content",
                            }}
                            aria-label={"Auto-armed focus protection, preset " + (focus.preset || "developer")}
                        >
                            <span aria-hidden="true">{"●"}</span>
                            {" Auto-armed"}
                            {focus.preset && (
                                <span style={{ opacity: 0.78 }}>
                                    {" · " + focus.preset}
                                </span>
                            )}
                        </div>
                    )}

                    <div style={S.bigRow}>
                        <span style={{ ...S.bigNum, color: stateColor }}>{focusMin}</span>
                        <span style={S.bigPct}>{focus.focusPct}%</span>
                    </div>
                    <div style={S.bigLabel}>min steady activity</div>

                    <div style={S.trackOuter}>
                        <div className="cortex-progress-motion" style={{
                            ...S.trackFill,
                            transform: `scaleX(${Math.max(Math.min(focus.focusPct, 100), 0) / 100})`,
                            background: stateColor,
                        }} />
                    </div>

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

            {/* Intervention preview */}
            {hasIntervention && (
                <InterventionCard
                    interventionId={interventionId}
                    failureBanner={interventionError}
                    prompt={interventionPrompt}
                    causalText={realCausal}
                    causalSignals={causalSignals}
                    whyOpen={whyOpen}
                    whyError={whyError}
                    onToggleWhy={() => {
                        if (!whyOpen && causalSignals.length === 0 && interventionId) {
                            safeSendMessage({
                                type: "WHY_DETAIL_REQUEST",
                                intervention_id: interventionId,
                            });
                        }
                        setWhyOpen(!whyOpen);
                    }}
                    microSteps={microSteps}
                    onToggleStep={handleMicroStepToggle}
                    closeTabs={visibleCloseTabs}
                    overflowCount={overflowCount}
                    keepCount={keepTabs.length}
                    onExpandTabs={() => setTabsExpanded(true)}
                    hasTabRecommendations={tabRecs !== null}
                    errorAnalysis={realErrAnalysis}
                    recommended={rec}
                    executable={executableRec}
                    manualCount={manualRec.length}
                    executionMode={executionMode}
                    manifestStatus={manifestForCurrent?.status ?? null}
                    canExecute={canExecutePresented}
                    apply={applyState}
                    onApply={handleApply}
                    onUndo={handleUndo}
                    ratingEligible={interventionLevel === "guided_mode"
                        || interventionLevel === "simplified_workspace"}
                    rating={rating}
                    ratingTextOpen={ratingTextOpen}
                    ratingText={ratingText}
                    onRate={handleRate}
                    onRatingTextChange={setRatingText}
                    onRatingTextSubmit={handleRatingTextSubmit}
                    onRatingTextCancel={() => { setRatingText(""); setRatingTextOpen(false); }}
                />
            )}

            {/* Biometrics row — no card, 1px separators above/below */}
            {connected && hr ? (
                <div style={S.bioRow}>
                    <div style={S.bioCol}>
                        <span style={S.bioLabel}>BPM</span>
                        <span style={S.bioVal} aria-label={`${Math.round(hr)} beats per minute`}>{Math.round(hr)}</span>
                    </div>
                    <div style={S.bioCol}>
                        <span style={S.bioLabel}>Blinks</span>
                        <span style={S.bioVal} aria-label={blink ? `${Math.round(blink)} blinks per minute` : "no blink rate data"}>{blink ? `${Math.round(blink)}/m` : "—"}</span>
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

            {/* Settings */}
            <div style={S.settingsArea}>
                <QuietModeControl
                    kind={quietModeKind}
                    remainingMin={quietModeDurationMin}
                    onSelect={handleQuietModeKind}
                />

                <div style={S.siteAccessRow}>
                    <div style={{ minWidth: 0, paddingRight: 12 }}>
                        <div style={S.toggleLabel}>Page context</div>
                        <div style={S.siteAccessDetail}>
                            {siteAccess === "granted"
                                ? "Body text allowed for this site · never incognito"
                                : siteAccess === "unavailable"
                                ? "Unavailable on this page"
                                : "Off for this site · page body stays out of context"}
                        </div>
                        {siteAccessError && (
                            <div role="status" style={S.siteAccessError}>
                                {siteAccessError}
                            </div>
                        )}
                    </div>
                    <button
                        className="cortex-ghost-btn"
                        data-testid="site-access-button"
                        style={{
                            ...S.siteAccessButton,
                            opacity: siteAccess === "busy"
                                || siteAccess === "checking"
                                || siteAccess === "unavailable"
                                ? 0.55
                                : 1,
                        }}
                        disabled={siteAccess === "busy"
                            || siteAccess === "checking"
                            || siteAccess === "unavailable"}
                        onClick={() => { void handleSiteAccess(); }}
                        aria-label={siteAccess === "granted"
                            ? "Revoke Cortex page context access"
                            : "Allow Cortex page context access"}
                    >
                        {siteAccess === "checking"
                            ? "Checking"
                            : siteAccess === "busy"
                            ? "Updating"
                            : siteAccess === "unavailable"
                            ? "Unavailable"
                            : siteAccess === "granted"
                            ? "Revoke"
                            : "Allow"}
                    </button>
                </div>

                <div style={{ ...S.toggleRow, marginTop: 8 }}>
                    <div style={{ minWidth: 0, paddingRight: 12 }}>
                        <span style={S.toggleLabel} id="cortex-newtab-label">Use the browser’s default new tab</span>
                        <div style={S.settingDetail}>
                            Off shows the Cortex Pulse Room on every new tab.
                        </div>
                    </div>
                    <button
                        style={{
                            ...S.toggleTrack,
                            background: newtabDisabled ? CX.accent : CX.borderEmphasis,
                        }}
                        onClick={handleNewtabToggle}
                        role="switch"
                        aria-checked={newtabDisabled}
                        aria-labelledby="cortex-newtab-label"
                        data-testid="newtab-default-switch"
                    >
                        <div style={{
                            ...S.toggleThumb,
                            transform: newtabDisabled ? "translateX(16px)" : "translateX(0)",
                        }} />
                    </button>
                </div>

                <div style={S.settingsDivider} aria-hidden="true" />
                <StopCortex
                    stopping={stopping}
                    stopRequested={stopRequested && !connected}
                    onStop={handleStopCortex}
                />
            </div>

            {/* Today footer — no card, lowest hierarchy */}
            {dailyStats && (
                <div style={S.todayFooter}>
                    <div style={S.todayCol}>
                        <span style={S.todayVal}>{Math.round(dailyStats.totalFocusMin)}m</span>
                        <span style={S.todayLabel}>STEADY</span>
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

            <TrendsMiniStrip />

            <div style={S.historyFooter}>
                <div style={S.historyLinkRow}>
                    <button
                        style={S.historyLink}
                        onClick={handleOpenDashboardHistory}
                        data-testid="view-history-link"
                        aria-label="Open History tab in desktop dashboard"
                    >View history <span aria-hidden="true">{"→"}</span></button>
                    <span
                        aria-hidden="true"
                        style={{ color: CX.textTertiary, fontSize: 11 }}
                    >·</span>
                    <button
                        style={S.historyLink}
                        onClick={() => setBugReportOpen(true)}
                        data-testid="report-bug-link"
                        aria-label="Report a bug"
                    >Report bug</button>
                </div>
                {historyStatus !== "" && (
                    <div
                        style={S.historyStatusLine}
                        data-testid="view-history-status"
                    >{historyStatus}</div>
                )}
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
                            color: CX.accentText,
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

            {/* Bug report modal. */}
            {bugReportOpen && (
                <div
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="cortex-bug-report-title"
                    aria-describedby="cortex-bug-report-description"
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
                            closeBugReport();
                        }
                    }}
                >
                    <div
                        ref={bugDialogRef}
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
                        <p
                            id="cortex-bug-report-description"
                            style={{
                                margin: "0 0 12px",
                                color: CX.textSecondary,
                                fontSize: 12,
                                lineHeight: 1.45,
                                fontFamily: CX.font,
                            }}
                        >Describe what happened, then review whether to include recent diagnostic logs.</p>
                        <textarea
                            ref={bugTextareaRef}
                            data-testid="bug-report-textarea"
                            value={bugReportText}
                            onChange={(e) => setBugReportText(e.target.value)}
                            placeholder="What happened? (10-500 chars)"
                            maxLength={500}
                            rows={5}
                            aria-label="Bug description"
                            style={{
                                width: "100%",
                                background: CX.tertiary,
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
                                    color: CX.danger,
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
                                onClick={closeBugReport}
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
                                    background: CX.text,
                                    border: "none",
                                    borderRadius: CX.radiusSm,
                                    color: CX.textInverse,
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

export default CortexPopup;
