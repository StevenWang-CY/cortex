/**
 * Cortex Chrome Extension — Popup UI
 *
 * Design: Cortex Visual Identity Guide — dark, calm, Linear/Claude-inspired.
 * Inter + JetBrains Mono typography, indigo accent, 4px grid spacing.
 * No emoji. No motivational copy. Sentence case everywhere.
 */

import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { CX, STATE_COLORS, STATE_LABELS, CX_KEYFRAMES } from "./design-tokens";

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
}

interface FocusSnapshot {
    elapsedMs: number;
    focusMs: number;
    focusPct: number;
    distractionsBlocked: number;
    longestStreakMin: number;
    currentStreakMs: number;
    goal: string;
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
 * Synthesize close_tab actions from tab_recommendations when the LLM
 * generated recommendations but no matching suggested_actions.
 */
function synthesizeActions(
    actions: Record<string, unknown>[],
    tabRecs: { tabs: Record<string, unknown>[]; summary: string } | null,
): Record<string, unknown>[] {
    if (!tabRecs || !tabRecs.tabs || tabRecs.tabs.length === 0) return actions;
    const hasClose = actions.some(a => a.action_type === "close_tab" || a.action_type === "bookmark_and_close");
    if (hasClose) return actions;
    const closeable = tabRecs.tabs.filter(t => t.action === "close" || t.action === "bookmark_and_close");
    if (closeable.length === 0) return actions;
    return [
        ...actions,
        ...closeable.map((t, i) => ({
            action_id: `synth_${Date.now()}_${i}`,
            action_type: t.action === "bookmark_and_close" ? "bookmark_and_close" : "close_tab",
            tab_index: typeof t.tab_index === "number" ? t.tab_index : Number(t.tab_index),
            target: "",
            label: `Close ${t.tab_title || "tab"}`,
            reason: t.reason || "",
            category: "recommended",
            reversible: true,
            metadata: {},
        })),
    ];
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

// --- Main ---

function CortexPopup(): React.ReactElement {
    const [connected, setConnected] = useState(false);
    const [state, setState] = useState<CortexState | null>(null);
    const [focus, setFocus] = useState<FocusSnapshot | null>(null);
    const [dailyStats, setDailyStats] = useState<DailyStats | null>(null);
    const [goalInput, setGoalInput] = useState("");
    const [alert, setAlert] = useState<{ title: string; body: string } | null>(null);
    const [activeActions, setActiveActions] = useState<Record<string, unknown>[]>([]);
    const [tabRecs, setTabRecs] = useState<{ tabs: Record<string, unknown>[]; summary: string } | null>(null);
    const [errAnalysis, setErrAnalysis] = useState<Record<string, string> | null>(null);
    const [interventionId, setInterventionId] = useState<string>("");
    const [applied, setApplied] = useState(false);
    const [causalExplanation, setCausalExplanation] = useState<string>("");
    const [briefing, setBriefing] = useState<MorningBriefing | null>(null);
    const [tabCloseDisabled, setTabCloseDisabled] = useState(false);
    const [quietMode, setQuietMode] = useState(false);
    const [launching, setLaunching] = useState(false);
    const [launchError, setLaunchError] = useState(false);
    const [tabsExpanded, setTabsExpanded] = useState(false);

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
        chrome.storage.session.get("quietMode", (result) => {
            if (result.quietMode === true) {
                setQuietMode(true);
            }
        });
    }, []);

    const handleTabCloseToggle = useCallback(() => {
        const newValue = !tabCloseDisabled;
        setTabCloseDisabled(newValue);
        chrome.storage.local.set({ cortex_tab_close_disabled: newValue });
    }, [tabCloseDisabled]);

    const handleQuietModeToggle = useCallback(() => {
        const newValue = !quietMode;
        setQuietMode(newValue);
        chrome.runtime.sendMessage({ type: "TOGGLE_QUIET_MODE", quiet: newValue });
    }, [quietMode]);

    const handleLaunchCortex = useCallback(() => {
        setLaunching(true);
        setLaunchError(false);
        chrome.runtime.sendMessage({ type: "LAUNCH_CORTEX" }, (resp) => {
            if (resp?.ok && resp.status === "camera_enabled") {
                setLaunching(false);
            } else {
                setLaunching(false);
                setLaunchError(true);
                setTimeout(() => setLaunchError(false), 5000);
            }
        });
    }, []);

    useEffect(() => {
        chrome.runtime.sendMessage({ type: "GET_STATE" }, (resp) => {
            if (!resp) return;
            setConnected(resp.connected);
            setState(resp.state);
            setFocus(resp.focusSession);
            if (resp.intervention) {
                const p = resp.intervention as Record<string, unknown>;
                const rawActions = (p.suggested_actions as Record<string, unknown>[]) || [];
                const recs = (p.tab_recommendations as { tabs: Record<string, unknown>[]; summary: string }) || null;
                setActiveActions(synthesizeActions(rawActions, recs));
                setTabRecs(recs);
                setErrAnalysis((p.error_analysis as Record<string, string>) || null);
                setInterventionId(String(p.intervention_id || ""));
                setApplied(false);
            }
        });
        chrome.runtime.sendMessage({ type: "GET_DAILY_STATS" }, (stats) => {
            if (stats) setDailyStats(stats);
        });
    }, []);

    useEffect(() => {
        const listener = (msg: Record<string, unknown>) => {
            switch (msg.type) {
                case "CONNECTION_CHANGED":
                    setConnected(msg.connected as boolean);
                    break;
                case "STATE_UPDATE":
                    setState(msg.payload as CortexState);
                    if (msg.focusSession) setFocus(msg.focusSession as FocusSnapshot);
                    break;
                case "FOCUS_SESSION_STARTED":
                    break;
                case "FOCUS_SESSION_ENDED":
                    setFocus(null);
                    chrome.runtime.sendMessage({ type: "GET_DAILY_STATS" }, (stats) => {
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
                    const recs = (p.tab_recommendations as { tabs: Record<string, unknown>[]; summary: string }) || null;
                    setActiveActions(synthesizeActions(rawActions, recs));
                    setTabRecs(recs);
                    setErrAnalysis((p.error_analysis as Record<string, string>) || null);
                    setInterventionId(String(p.intervention_id || ""));
                    setCausalExplanation(String(p.causal_explanation || ""));
                    setApplied(false);
                    break;
                }
                case "INTERVENTION_RESTORE":
                    setActiveActions([]);
                    setTabRecs(null);
                    setErrAnalysis(null);
                    setCausalExplanation("");
                    setApplied(false);
                    break;
                case "SETTINGS_SYNC": {
                    const settings = msg.payload as Record<string, unknown>;
                    if (typeof settings.quiet_mode === "boolean") {
                        setQuietMode(settings.quiet_mode);
                    }
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
            }
        };
        chrome.runtime.onMessage.addListener(listener);
        return () => chrome.runtime.onMessage.removeListener(listener);
    }, []);

    const handleConnect = useCallback(() => {
        chrome.runtime.sendMessage({ type: "CONNECT" });
    }, []);

    const handleStartFocus = useCallback(() => {
        chrome.runtime.sendMessage({
            type: "START_FOCUS",
            goal: goalInput || "Study session",
        });
        setGoalInput("");
    }, [goalInput]);

    const handleStopFocus = useCallback(() => {
        chrome.runtime.sendMessage({ type: "STOP_FOCUS" });
    }, []);

    // Derived
    const stateStr = state?.state ?? "";
    const stateColor = STATE_COLORS[stateStr] || CX.textTertiary;
    const stateLabel = STATE_LABELS[stateStr] || "Idle";
    const hr = state?.biometrics?.heart_rate;
    const hrv = state?.biometrics?.hrv_rmssd;
    const blink = state?.biometrics?.blink_rate;

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

    const hasIntervention = activeActions.length > 0 || tabRecs || realErrAnalysis;

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
                <span style={S.logoText}>Cortex</span>
                {!connected ? (
                    <button style={S.connectBtn} onClick={handleConnect}>CONNECT</button>
                ) : (
                    <div style={S.statusRow} aria-live="polite">
                        <div style={getStateDotStyle(stateStr, stateColor)} />
                        <span style={{ ...S.statusLabel, color: stateColor }}>{stateLabel}</span>
                    </div>
                )}
            </div>

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
                            if (briefing.left_off_at) {
                                chrome.runtime.sendMessage({ type: "START_FOCUS", goal: briefing.left_off_at });
                            }
                            setBriefing(null);
                        }}>Resume</button>
                    </div>
                </div>
            )}

            {/* Disconnected state — centered, quiet notice */}
            {!connected && (
                <div style={S.disconnectedArea}>
                    <div style={{ width: 12, height: 12, borderRadius: "50%", border: `1.5px solid ${CX.textTertiary}`, flexShrink: 0 }} />
                    <div style={S.disconnectedTitle}>Not connected</div>
                    <div style={S.disconnectedBody}>Start the Cortex daemon</div>
                    <button
                        style={{
                            ...S.ghostBtn,
                            marginTop: 12,
                            opacity: launching ? 0.5 : 1,
                            pointerEvents: launching ? "none" as const : "auto" as const,
                        }}
                        onClick={handleLaunchCortex}
                        disabled={launching}
                    >
                        {launchError
                            ? "Daemon not running"
                            : launching
                                ? "Starting\u2026"
                                : "Start Cortex"}
                    </button>
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

            {/* Intervention preview */}
            {hasIntervention && (
                <div style={S.interventionCard}>
                    {/* Causal explanation */}
                    {realCausal && (
                        <div style={S.causalText}>{realCausal}</div>
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
                                style={applied ? { ...S.primaryBtn, ...S.doneBtnStyle } : S.primaryBtn}
                                disabled={applied}
                                onClick={() => {
                                    chrome.runtime.sendMessage({
                                        type: "EXECUTE_ALL_RECOMMENDED",
                                        actions: rec,
                                        intervention_id: interventionId,
                                    }, (results: Array<{ success: boolean }> | undefined) => {
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
                                    });
                                }}
                            >
                                {applied
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
                                            chrome.runtime.sendMessage(
                                                { type: "UNDO_ALL_RECENT", intervention_id: interventionId },
                                                () => setApplied(false),
                                            );
                                        }}
                                    >Undo</button>
                                </div>
                            )}
                        </>
                    )}
                </div>
            )}

            {/* Biometrics row — no card, 1px separators above/below */}
            {connected && (
                <div style={S.bioRow}>
                    <div style={S.bioCol}>
                        <span style={{ ...S.bioLabel, color: `${CX.bioHr}80` }}>BPM</span>
                        <span style={S.bioVal} aria-label={hr ? `${Math.round(hr)} beats per minute` : "no heart rate data"}>{hr ? Math.round(hr) : "\u2014"}</span>
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
            )}

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
        </div>
    );
}

// --- Styles ---

const S: Record<string, React.CSSProperties> = {
    root: {
        width: 380,
        maxHeight: 540,
        overflowY: "auto",
        padding: "0 20px 20px 20px",
        fontFamily: CX.font,
        fontSize: 13,
        color: CX.text,
        background: CX.bg,
    },

    // Alert toast
    alertBox: {
        padding: "12px 14px",
        borderRadius: CX.radiusMd,
        background: CX.surface,
        marginBottom: 12,
        animation: "cxAlertIn 0.2s cubic-bezier(0, 0, 0.2, 1)",
    },
    alertTitle: { fontSize: 13, fontWeight: 500, marginBottom: 2, color: CX.text },
    alertBody: { fontSize: 10, color: CX.textTertiary, lineHeight: 1.4 },

    // Header — 44px total
    header: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        height: 44,
        padding: "0",
        marginBottom: 0,
        borderBottom: `1px solid ${CX.borderDefault}`,
    },
    logoText: {
        fontSize: 15,
        fontWeight: 600,
        letterSpacing: "-0.02em",
        color: CX.text,
    },
    connectBtn: {
        padding: "4px 12px",
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusSm,
        background: "transparent",
        color: CX.textSecondary,
        cursor: "pointer",
        fontSize: 11,
        fontWeight: 500,
        fontFamily: CX.font,
        letterSpacing: "0.04em",
        textTransform: "uppercase" as const,
    },
    statusRow: { display: "flex", alignItems: "center", gap: 6 },
    statusLabel: {
        fontSize: 11,
        fontWeight: 500,
        letterSpacing: "0.04em",
        textTransform: "uppercase" as const,
        fontFamily: CX.font,
        transition: `color ${CX.durationSlow} ${CX.easeDefault}`,
    },

    // Disconnected area — centered vertically
    disconnectedArea: {
        display: "flex",
        flexDirection: "column" as const,
        alignItems: "center",
        justifyContent: "center",
        padding: "32px 0",
        gap: 4,
    },
    disconnectedTitle: { fontSize: 15, fontWeight: 600, color: CX.textSecondary, letterSpacing: "-0.015em" },
    disconnectedBody: { fontSize: 13, color: CX.textTertiary },

    // Morning briefing
    briefingCard: {
        background: CX.surface,
        borderRadius: CX.radiusLg,
        padding: 16,
        marginTop: CX.space6,
        borderLeft: `3px solid ${CX.accent}`,
    },
    briefingTitle: { fontSize: 15, fontWeight: 600, letterSpacing: "-0.015em", color: CX.text },
    briefingBody: { fontSize: 13, color: CX.textSecondary, lineHeight: 1.5, marginTop: 4 },

    // Ghost button
    ghostBtn: {
        padding: "6px 16px",
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusMd,
        background: "transparent",
        color: CX.textSecondary,
        cursor: "pointer",
        fontSize: 11,
        fontWeight: 500,
        fontFamily: CX.font,
        letterSpacing: "0.04em",
        textTransform: "uppercase" as const,
    },

    // Goal input — tertiary bg, 40px height, enter icon
    goalInput: {
        width: "100%",
        height: 40,
        padding: "0 32px 0 12px",
        border: "none",
        borderRadius: CX.radiusMd,
        background: CX.tertiary,
        color: CX.text,
        fontSize: 13,
        letterSpacing: "-0.005em",
        outline: "none",
        boxSizing: "border-box" as const,
        fontFamily: CX.font,
        marginTop: CX.space6,
    },
    goalEnterIcon: {
        position: "absolute" as const,
        right: 12,
        top: CX.space6 + 10,
        color: CX.textTertiary,
        fontSize: 14,
        pointerEvents: "none" as const,
    },

    // Session card — surface bg, no visible border
    sessionCard: {
        background: CX.surface,
        borderRadius: CX.radiusLg,
        padding: 16,
        marginTop: CX.space6,
    },
    focusHeader: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 16,
    },
    focusTitle: { fontSize: 15, fontWeight: 600, color: CX.text, letterSpacing: "-0.015em" },
    focusDuration: { fontSize: 13, color: CX.textTertiary },
    endBtn: {
        padding: "4px 12px",
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusSm,
        background: "transparent",
        color: CX.textSecondary,
        cursor: "pointer",
        fontSize: 11,
        fontWeight: 500,
        fontFamily: CX.font,
        letterSpacing: "0.04em",
        textTransform: "uppercase" as const,
    },
    bigRow: {
        display: "flex",
        alignItems: "baseline",
        justifyContent: "space-between",
        marginBottom: 4,
    },
    bigNum: {
        fontSize: 28,
        fontWeight: 600,
        letterSpacing: "-0.03em",
        lineHeight: 1.15,
        fontFamily: CX.mono,
    },
    bigPct: {
        fontSize: 16,
        fontWeight: 500,
        fontFamily: CX.mono,
        color: CX.textTertiary,
    },
    bigLabel: {
        fontSize: 13,
        color: CX.textSecondary,
        letterSpacing: "-0.005em",
        marginBottom: 12,
    },

    // Progress track — 6px tall
    trackOuter: {
        height: 6,
        borderRadius: CX.radiusSm,
        background: CX.tertiary,
        marginBottom: 16,
        overflow: "hidden",
    },
    trackFill: {
        height: "100%",
        borderRadius: CX.radiusSm,
        transition: `width 1s ease, background ${CX.durationSlow} ${CX.easeDefault}`,
    },

    // Stats row — three columns
    statsRow: { display: "flex", justifyContent: "space-around" },
    statCol: { display: "flex", flexDirection: "column" as const, alignItems: "center", gap: 2 },
    statVal: { fontSize: 16, fontWeight: 500, color: CX.text, fontFamily: CX.mono },
    statLabel: { fontSize: 11, fontWeight: 400, color: CX.textTertiary, letterSpacing: "0.04em", textTransform: "uppercase" as const },

    // Intervention preview — left border for HYPER
    interventionCard: {
        background: CX.surface,
        borderRadius: CX.radiusLg,
        padding: 16,
        marginTop: CX.space6,
        borderLeft: `3px solid ${STATE_COLORS.HYPER}`,
    },
    causalText: {
        fontSize: 13,
        color: CX.textSecondary,
        lineHeight: 1.5,
        marginBottom: 12,
        fontStyle: "italic",
        letterSpacing: "-0.005em",
    },
    tabRow: { display: "flex", alignItems: "center", gap: 8, height: 32 },
    tabXMark: { color: `${CX.danger}99`, fontSize: 12, fontWeight: 400, width: 14, textAlign: "center" as const, flexShrink: 0, fontFamily: CX.mono },
    tabName: {
        fontSize: 12, color: CX.text, fontFamily: CX.mono,
        whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis" as const,
    },
    keepLine: { fontSize: 10, color: CX.textTertiary, marginTop: 6 },

    // Error — tertiary bg, not danger
    errBox: {
        padding: 12,
        background: CX.tertiary,
        borderRadius: CX.radiusMd,
        marginBottom: 12,
    },
    errBody: { fontSize: 12, color: CX.text, lineHeight: 1.5, fontFamily: CX.mono },
    errCode: {
        fontSize: 12, color: CX.accent, marginTop: 8, fontFamily: CX.mono,
        lineHeight: 1.5, whiteSpace: "pre-wrap" as const, border: "none", margin: 0,
        padding: 0, background: "none",
    },

    // Primary CTA — full width, accent bg, 40px height
    primaryBtn: {
        width: "100%",
        height: 40,
        padding: "0 20px",
        border: "none",
        borderRadius: CX.radiusMd,
        background: CX.accent,
        color: CX.textInverse,
        fontSize: 11,
        fontWeight: 500,
        cursor: "pointer",
        letterSpacing: "0.04em",
        textTransform: "uppercase" as const,
        fontFamily: CX.font,
        transition: `background ${CX.durationFast} ${CX.easeDefault}`,
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
        marginTop: 8, fontSize: 10, color: CX.textTertiary,
    },
    undoLink: {
        background: "none", border: "none", color: CX.accent, fontSize: 10,
        fontWeight: 500, cursor: "pointer", padding: 0, fontFamily: CX.font,
    },

    // Biometrics row — no card, 1px separators
    bioRow: {
        display: "flex",
        justifyContent: "space-around",
        padding: "12px 0",
        marginTop: CX.space6,
        borderTop: `1px solid ${CX.borderDefault}`,
        borderBottom: `1px solid ${CX.borderDefault}`,
    },
    bioCol: { display: "flex", alignItems: "center", gap: 6 },
    bioLabel: {
        fontSize: 11,
        fontWeight: 500,
        letterSpacing: "0.04em",
        textTransform: "uppercase" as const,
    },
    bioVal: {
        fontSize: 12,
        fontFamily: CX.mono,
        color: CX.text,
    },

    // Settings — no card, separator above
    settingsArea: {
        padding: "16px 0",
        borderTop: `1px solid ${CX.borderDefault}`,
        marginTop: CX.space6,
    },
    toggleRow: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
    },
    toggleLabel: { fontSize: 13, color: CX.textSecondary },
    toggleTrack: {
        position: "relative" as const,
        width: 36,
        height: 20,
        borderRadius: 10,
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
        width: 16,
        height: 16,
        borderRadius: "50%",
        background: "#fff",
        transition: `transform ${CX.durationNormal} ${CX.easeDefault}`,
    },

    // Today footer — no card, visual basement
    todayFooter: {
        display: "flex",
        justifyContent: "space-around",
        padding: "16px 0 0 0",
        borderTop: `1px solid ${CX.borderDefault}`,
        marginTop: CX.space6,
    },
    todayCol: { display: "flex", flexDirection: "column" as const, alignItems: "center", gap: 2 },
    todayVal: { fontSize: 12, fontFamily: CX.mono, color: CX.textSecondary },
    todayLabel: { fontSize: 10, color: CX.textTertiary, letterSpacing: "0.02em", textTransform: "uppercase" as const },
};

// --- Mount ---

const root = document.getElementById("root");
if (root) {
    createRoot(root).render(<CortexPopup />);
}

export default CortexPopup;
