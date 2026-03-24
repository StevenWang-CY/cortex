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

    const [launchStatus, setLaunchStatus] = useState("");

    const handleLaunchCortex = useCallback(() => {
        setLaunching(true);
        setLaunchError(false);
        setLaunchStatus("Launching daemon\u2026");
        chrome.runtime.sendMessage({ type: "LAUNCH_CORTEX" }, (resp) => {
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

    const [stopping, setStopping] = useState(false);
    const handleStopCortex = useCallback(async () => {
        setStopping(true);
        // Force local UI to disconnected immediately
        setConnected(false);
        setState(null);
        setFocus(null);
        // Tell background to disconnect WS, kill daemon via HTTP, close tabs
        chrome.runtime.sendMessage({ type: "STOP_CORTEX" });
        // Wait a moment for shutdown to propagate, then release button
        setTimeout(() => setStopping(false), 2000);
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

            {/* Disconnected state — centered, quiet notice with one-click launch */}
            {!connected && (
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
                    <div style={S.disconnectedTitle}>{launching ? "Starting Cortex" : "Not connected"}</div>
                    <div style={S.disconnectedBody}>
                        {launchStatus || "Launch daemon with camera"}
                    </div>
                    <button
                        style={{
                            ...S.primaryBtn,
                            marginTop: 16,
                            opacity: launching ? 0.5 : 1,
                            pointerEvents: launching ? "none" as const : "auto" as const,
                            maxWidth: 200,
                        }}
                        onClick={handleLaunchCortex}
                        disabled={launching}
                    >
                        {launchError
                            ? "Retry"
                            : launching
                                ? "Starting\u2026"
                                : "Start Cortex"}
                    </button>
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
        transition: "background 0.2s ease",
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

    // Biometrics row
    bioRow: {
        display: "flex",
        justifyContent: "space-between",
        padding: "16px 8px",
        marginBottom: 16,
        borderTop: `1px solid ${CX.borderDefault}`,
        borderBottom: `1px solid ${CX.borderDefault}`,
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
        borderRadius: 12,
        border: "none",
        cursor: "pointer",
        padding: 0,
        flexShrink: 0,
        transition: `background 0.2s ease`,
    },
    toggleThumb: {
        position: "absolute" as const,
        top: 2,
        left: 2,
        width: 20,
        height: 20,
        borderRadius: "50%",
        background: "#fff",
        transition: `transform 0.2s ease`,
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
};

export default CortexPopup;
