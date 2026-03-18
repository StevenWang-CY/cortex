/**
 * Cortex Chrome Extension — Popup UI
 *
 * Design: Cortex Design System — dark, calm, Linear/Raycast-inspired.
 * Inter + JetBrains Mono typography, indigo accent, 4px grid spacing.
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
        width: 6,
        height: 6,
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
    const [launching, setLaunching] = useState(false);
    const [launchError, setLaunchError] = useState(false);
    const [tabsExpanded, setTabsExpanded] = useState(false);

    // Inject fonts + keyframes (single injection point)
    useEffect(() => {
        const id = "cortex-popup-styles";
        if (document.getElementById(id)) return;
        const style = document.createElement("style");
        style.id = id;
        style.textContent = CX_KEYFRAMES;
        document.head.appendChild(style);
        return () => { style.remove(); };
    }, []);

    // Load tab-close toggle state on mount
    useEffect(() => {
        chrome.storage.local.get("cortex_tab_close_disabled", (result) => {
            if (result.cortex_tab_close_disabled === true) {
                setTabCloseDisabled(true);
            }
        });
    }, []);

    const handleTabCloseToggle = useCallback(() => {
        const newValue = !tabCloseDisabled;
        setTabCloseDisabled(newValue);
        chrome.storage.local.set({ cortex_tab_close_disabled: newValue });
    }, [tabCloseDisabled]);

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
            // Load active intervention if one exists
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
                    setTimeout(() => setAlert(null), 6000);
                    break;
                case "BREAK_SUGGESTED":
                    setAlert({ title: "Time for a break", body: msg.reason as string });
                    setTimeout(() => setAlert(null), 8000);
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

    // Cap visible tabs at 5, expandable on click
    const visibleCloseTabs = tabsExpanded ? closeTabs : closeTabs.slice(0, 5);
    const overflowCount = tabsExpanded ? 0 : closeTabs.length - visibleCloseTabs.length;

    // Filter out generic error_analysis that has no real content
    const genericErrPhrases = ["no specific errors", "no errors detected", "not applicable", "no error", "n/a"];
    const realErrAnalysis = errAnalysis?.root_cause && !genericErrPhrases.some(
        p => (errAnalysis.root_cause ?? "").toLowerCase().includes(p)
    ) ? errAnalysis : null;

    // Filter out generic causal explanation
    const realCausal = causalExplanation && causalExplanation.length > 20
        && /\d/.test(causalExplanation) ? causalExplanation : "";

    const hasIntervention = activeActions.length > 0 || tabRecs || realErrAnalysis;

    return (
        <div style={S.root}>
            {/* Alert */}
            {alert && (
                <div style={S.alertBox}>
                    <div style={S.alertTitle}>{alert.title}</div>
                    <div style={S.alertBody}>{alert.body}</div>
                </div>
            )}

            {/* Header */}
            <div style={S.header}>
                <div style={S.logoRow}>
                    <div style={S.logoMark} />
                    <span style={S.logoText}>cortex</span>
                </div>
                {!connected ? (
                    <button style={S.connectBtn} onClick={handleConnect}>Connect</button>
                ) : (
                    <div style={S.statusRow} aria-live="polite">
                        <div style={getStateDotStyle(stateStr, stateColor)} />
                        <span style={{ ...S.statusText, color: stateColor }}>{stateLabel}</span>
                    </div>
                )}
            </div>

            {/* Not connected banner */}
            {!connected && (
                <div style={S.disconnectedBanner}>
                    <div style={S.disconnectedTitle}>Not connected</div>
                    <div style={S.disconnectedBody}>Start the Cortex daemon to begin</div>
                </div>
            )}

            {/* Launch / Camera */}
            <button
                style={{
                    ...S.primaryBtn,
                    marginBottom: 12,
                    background: launchError ? CX.dangerDim
                        : launching ? CX.tertiary
                        : connected ? CX.surface : CX.accent,
                    color: launchError ? CX.danger
                        : launching ? CX.textSecondary
                        : connected ? CX.text : CX.textInverse,
                    border: connected && !launchError ? `1px solid ${CX.border}` : "none",
                    cursor: launching ? "default" : "pointer",
                    pointerEvents: launching ? "none" as const : "auto" as const,
                }}
                onClick={handleLaunchCortex}
                disabled={launching}
            >
                {launchError
                    ? "Daemon not running \u2014 run cortex-dev first"
                    : launching
                        ? "Starting\u2026"
                        : connected
                            ? "Restart Camera"
                            : "Start Cortex daemon"}
            </button>

            {/* Morning Briefing */}
            {briefing && (
                <div style={{ ...S.card, borderColor: "rgba(129, 140, 248, 0.15)" }}>
                    <div style={S.sectionHead}>Where you left off</div>
                    <div style={{ fontSize: 13, color: CX.text, lineHeight: 1.5, marginBottom: 8 }}>{briefing.summary}</div>
                    {briefing.action_items.length > 0 && (
                        <div style={{ marginBottom: 8 }}>
                            {briefing.action_items.map((item, i) => (
                                <div key={i} style={{ ...S.tabRow, padding: "2px 0" }}>
                                    <span style={{ ...S.tabXMark, color: CX.accent }}>{i + 1}.</span>
                                    <span style={{ fontSize: 11, color: CX.textSecondary }}>{item}</span>
                                </div>
                            ))}
                        </div>
                    )}
                    <button
                        style={{ ...S.primaryBtn, fontSize: 11, padding: "8px 0" }}
                        onClick={() => setBriefing(null)}
                    >Got it</button>
                </div>
            )}

            {/* Start Focus */}
            {connected && !focus && (
                <div style={S.section}>
                    <input
                        style={S.input}
                        placeholder="What are you working on?"
                        value={goalInput}
                        onChange={(e) => setGoalInput(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && handleStartFocus()}
                    />
                    <button style={S.primaryBtn} onClick={handleStartFocus}>
                        Start session
                    </button>
                </div>
            )}

            {/* Active Focus — sticky so it stays visible when scrolling */}
            {focus && (
                <div style={{ ...S.card, position: "sticky" as const, top: 0, zIndex: 10 }}>
                    <div style={S.focusHeader}>
                        <div>
                            <div style={S.focusGoal}>{focus.goal}</div>
                            <div style={S.muted}>{elapsedMin}m elapsed</div>
                        </div>
                        <button style={S.endBtn} onClick={handleStopFocus}>End</button>
                    </div>

                    <div style={S.bigRow}>
                        <span style={{ ...S.bigNum, color: stateColor }}>{focusMin}</span>
                        <div>
                            <div style={S.bigLabel}>min focused</div>
                            <div style={S.muted}>{focus.focusPct}%</div>
                        </div>
                    </div>

                    <div style={S.trackOuter}>
                        <div style={{
                            ...S.trackFill,
                            width: `${Math.min(focus.focusPct, 100)}%`,
                            background: stateColor,
                        }} />
                    </div>

                    <div style={S.metricsRow}>
                        <Metric label="streak" value={streakMin > 0 ? `${streakMin}:${String(streakRemSec).padStart(2, "0")}` : `${streakSec}s`} />
                        <div style={S.metricDiv} />
                        <Metric label="blocked" value={String(focus.distractionsBlocked)} />
                        <div style={S.metricDiv} />
                        <Metric label="best" value={`${focus.longestStreakMin}m`} />
                    </div>
                </div>
            )}

            {/* Biometrics */}
            {connected && (
                <div style={S.card}>
                    <div style={S.metricsRow}>
                        <Metric label="bpm" value={hr ? String(Math.round(hr)) : "--"} labelColor={CX.bioHr} ariaLabel={hr ? `${Math.round(hr)} beats per minute` : "no heart rate data"} />
                        <div style={S.metricDiv} />
                        <Metric label="hrv" value={hrv ? String(Math.round(hrv)) : "--"} labelColor={CX.bioHrv} ariaLabel={hrv ? `${Math.round(hrv)} milliseconds heart rate variability` : "no HRV data"} />
                        <div style={S.metricDiv} />
                        <Metric label="blinks" value={blink ? String(Math.round(blink)) : "--"} labelColor={CX.bioBlink} ariaLabel={blink ? `${Math.round(blink)} blinks per minute` : "no blink rate data"} />
                    </div>
                </div>
            )}

            {/* Intervention */}
            {hasIntervention && (
                <div style={{ ...S.card, borderColor: CX.borderMed }}>
                    {/* Causal explanation */}
                    {realCausal && (
                        <div style={{ fontSize: 13, color: CX.textSecondary, lineHeight: 1.5, marginBottom: 12, fontStyle: "italic" }}>
                            {realCausal}
                        </div>
                    )}

                    {visibleCloseTabs.length > 0 && (
                        <div style={{ marginBottom: 12 }}>
                            <div style={S.sectionHead}>Closing {closeTabs.length} tab{closeTabs.length !== 1 ? "s" : ""}</div>
                            {visibleCloseTabs.map((t, i) => {
                                const title = String(t.tab_title || "Untitled");
                                const rawReason = String(t.reason || "");
                                const reason = genericReasonPhrases.some(p => rawReason.toLowerCase().includes(p)) ? "" : rawReason;
                                return (
                                    <div key={`c${i}`} style={{ padding: "3px 0" }}>
                                        <div style={S.tabRow}>
                                            <span style={S.tabXMark}>{"\u00d7"}</span>
                                            <span style={S.tabName}>{title}</span>
                                        </div>
                                        {reason && (
                                            <div style={{ fontSize: 10, color: CX.textTertiary, marginLeft: 22, lineHeight: 1.3 }}>{reason}</div>
                                        )}
                                    </div>
                                );
                            })}
                            {overflowCount > 0 && (
                                <button
                                    style={{ fontSize: 11, color: CX.accent, marginTop: 4, background: "none", border: "none", cursor: "pointer", padding: 0, fontFamily: CX.font }}
                                    onClick={() => setTabsExpanded(true)}
                                >+{overflowCount} more</button>
                            )}
                            {keepTabs.length > 0 && (
                                <div style={S.keepLine}>Keeping <span style={{ color: STATE_COLORS.FLOW }}>{keepTabs.length}</span> you need</div>
                            )}
                        </div>
                    )}

                    {realErrAnalysis && realErrAnalysis.root_cause && (
                        <div style={S.errBox}>
                            <div style={S.errHead}>Error</div>
                            <div style={S.errBody}>{realErrAnalysis.root_cause}</div>
                            {realErrAnalysis.suggested_fix && (
                                <pre style={S.errCode}>{realErrAnalysis.suggested_fix}</pre>
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
                                            }, 1500);
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

            {/* Settings */}
            <div style={S.card}>
                <div style={S.toggleRow}>
                    <div>
                        <div style={S.toggleLabel}>Tab closing</div>
                        <div style={S.toggleDesc}>
                            {tabCloseDisabled ? "Cortex won\u2019t close tabs" : "Cortex can close distracting tabs"}
                        </div>
                    </div>
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
            </div>

            {/* Daily Stats */}
            {dailyStats && (
                <div style={S.card}>
                    <div style={S.sectionHead}>Today</div>
                    <div style={S.dailyGrid}>
                        <Metric label="focus" value={String(Math.round(dailyStats.totalFocusMin))} unit="m" />
                        <Metric label="sessions" value={String(dailyStats.sessions)} />
                        <Metric label="best" value={String(Math.round(dailyStats.longestStreakMin))} unit="m" />
                        <Metric label="blocked" value={String(dailyStats.distractionsBlocked)} />
                    </div>
                </div>
            )}
        </div>
    );
}

// --- Metric Component ---

function Metric({ label, value, unit, labelColor, ariaLabel }: {
    label: string;
    value: string;
    unit?: string;
    labelColor?: string;
    ariaLabel?: string;
}): React.ReactElement {
    return (
        <div style={S.metric} aria-label={ariaLabel}>
            <span style={S.metricVal}>
                {value}
                {unit && <span style={S.metricUnit}>{unit}</span>}
            </span>
            <span style={{ ...S.metricLabel, color: labelColor ? `${labelColor}99` : CX.textTertiary }}>{label}</span>
        </div>
    );
}

// --- Styles ---

const S: Record<string, React.CSSProperties> = {
    root: {
        width: 380,
        maxHeight: 540,
        overflowY: "auto",
        padding: 16,
        fontFamily: CX.font,
        fontSize: 13,
        color: CX.text,
        background: CX.bg,
    },

    // Alert
    alertBox: {
        padding: "12px 14px",
        borderRadius: CX.radiusMd,
        background: CX.surface,
        border: `1px solid ${CX.border}`,
        marginBottom: 12,
    },
    alertTitle: { fontSize: 13, fontWeight: 600, marginBottom: 2, color: CX.text },
    alertBody: { fontSize: 11, color: CX.textSecondary, lineHeight: 1.5 },

    // Header
    header: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 16,
        paddingBottom: 12,
        borderBottom: `1px solid ${CX.border}`,
    },
    logoRow: { display: "flex", alignItems: "center", gap: 8 },
    logoMark: {
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: `linear-gradient(135deg, ${CX.accent}, ${CX.accentHover})`,
    },
    logoText: {
        fontSize: 13,
        fontWeight: 600,
        letterSpacing: -0.3,
        color: CX.text,
    },
    connectBtn: {
        padding: "4px 12px",
        border: `1px solid ${CX.borderMed}`,
        borderRadius: CX.radiusSm,
        background: "transparent",
        color: CX.textSecondary,
        cursor: "pointer",
        fontSize: 11,
        fontWeight: 500,
        fontFamily: CX.font,
    },
    statusRow: { display: "flex", alignItems: "center", gap: 6 },
    statusText: {
        fontSize: 11,
        fontWeight: 500,
        fontFamily: CX.mono,
        letterSpacing: 0.5,
        transition: `color ${CX.durationSlow} ${CX.easeDefault}`,
    },

    // Disconnected banner
    disconnectedBanner: {
        padding: "12px 14px",
        borderRadius: CX.radiusMd,
        background: CX.tertiary,
        border: `1px solid ${CX.border}`,
        marginBottom: 12,
        textAlign: "center" as const,
    },
    disconnectedTitle: { fontSize: 13, fontWeight: 600, color: CX.textSecondary, marginBottom: 2 },
    disconnectedBody: { fontSize: 11, color: CX.textTertiary, lineHeight: 1.4 },

    // Sections
    section: { marginBottom: 12 },
    input: {
        width: "100%",
        padding: "10px 12px",
        border: `1px solid ${CX.border}`,
        borderRadius: CX.radiusMd,
        background: CX.surface,
        color: CX.text,
        fontSize: 13,
        marginBottom: 8,
        outline: "none",
        boxSizing: "border-box" as const,
        fontFamily: CX.font,
    },
    primaryBtn: {
        width: "100%",
        padding: "10px 20px",
        border: "none",
        borderRadius: CX.radiusMd,
        background: CX.accent,
        color: CX.textInverse,
        fontSize: 11,
        fontWeight: 500,
        cursor: "pointer",
        letterSpacing: 0.5,
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

    // Card
    card: {
        background: CX.surface,
        borderRadius: CX.radiusLg,
        padding: 16,
        marginBottom: 8,
        border: `1px solid ${CX.border}`,
    },

    // Focus
    focusHeader: {
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        marginBottom: 16,
    },
    focusGoal: { fontSize: 13, fontWeight: 600, color: CX.text, letterSpacing: -0.2 },
    muted: { fontSize: 10, color: CX.textSecondary, marginTop: 2, fontFamily: CX.mono },
    endBtn: {
        padding: "4px 12px",
        border: `1px solid ${CX.dangerDim}`,
        borderRadius: CX.radiusSm,
        background: CX.dangerDim,
        color: CX.danger,
        cursor: "pointer",
        fontSize: 10,
        fontWeight: 600,
        fontFamily: CX.font,
    },
    bigRow: { display: "flex", alignItems: "baseline", gap: 8, marginBottom: 12 },
    bigNum: {
        fontSize: 28,
        fontWeight: 600,
        letterSpacing: -1,
        lineHeight: 1.15,
        fontFamily: CX.mono,
    },
    bigLabel: { fontSize: 13, color: CX.textSecondary },

    // Progress track
    trackOuter: {
        height: 2,
        borderRadius: 1,
        background: CX.border,
        marginBottom: 16,
        overflow: "hidden",
    },
    trackFill: {
        height: "100%",
        borderRadius: 1,
        transition: `width 1s ease, background ${CX.durationSlow} ${CX.easeDefault}`,
    },

    // Metrics row
    metricsRow: { display: "flex", alignItems: "center", justifyContent: "space-around" },
    metric: { display: "flex", flexDirection: "column" as const, alignItems: "center", gap: 2 },
    metricVal: { fontSize: 15, fontWeight: 400, color: CX.text, fontFamily: CX.mono, transition: `all 0.3s ${CX.easeDefault}` },
    metricUnit: { fontSize: 10, color: CX.textSecondary, marginLeft: 1 },
    metricLabel: { fontSize: 9, color: CX.textTertiary, letterSpacing: 0.8, fontFamily: CX.mono, textTransform: "uppercase" as const },
    metricDiv: { width: 1, height: 16, background: CX.border },

    // Intervention
    sectionHead: { fontSize: 11, fontWeight: 500, color: CX.textSecondary, marginBottom: 8, letterSpacing: 0.2 },
    tabRow: { display: "flex", alignItems: "center", gap: 8, padding: "3px 0" },
    tabXMark: { color: CX.danger, fontSize: 13, fontWeight: 500, width: 14, textAlign: "center" as const, flexShrink: 0, fontFamily: CX.mono },
    tabName: {
        fontSize: 12, color: CX.textSecondary,
        whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis" as const,
    },
    keepLine: { fontSize: 11, color: CX.textTertiary, marginTop: 6 },

    // Error
    errBox: {
        padding: "12px 14px",
        background: CX.dangerDim,
        borderRadius: CX.radiusMd,
        border: `1px solid rgba(239, 68, 68, 0.08)`,
        marginBottom: 12,
    },
    errHead: { fontSize: 10, fontWeight: 600, color: CX.danger, marginBottom: 4, fontFamily: CX.mono, letterSpacing: 0.5, textTransform: "uppercase" as const },
    errBody: { fontSize: 13, color: CX.text, lineHeight: 1.5 },
    errCode: {
        fontSize: 12, color: CX.textSecondary, marginTop: 8, fontFamily: CX.mono,
        padding: "8px 10px", background: "rgba(0,0,0,.3)", borderRadius: CX.radiusSm, lineHeight: 1.5,
        whiteSpace: "pre-wrap" as const, border: "none", margin: 0,
    },

    // Undo
    undoRow: {
        display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
        marginTop: 8, fontSize: 11, color: CX.textTertiary,
    },
    undoLink: {
        background: "none", border: "none", color: CX.accent, fontSize: 11,
        fontWeight: 500, cursor: "pointer", padding: 0, fontFamily: CX.font,
    },

    // Daily stats
    dailyGrid: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 4 },

    // Toggle
    toggleRow: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
    },
    toggleLabel: { fontSize: 13, fontWeight: 500, color: CX.text },
    toggleDesc: { fontSize: 10, color: CX.textTertiary, marginTop: 2 },
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
};

// --- Mount ---

const root = document.getElementById("root");
if (root) {
    createRoot(root).render(<CortexPopup />);
}

export default CortexPopup;
