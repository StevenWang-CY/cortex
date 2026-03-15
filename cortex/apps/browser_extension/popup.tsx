/**
 * Cortex Chrome Extension — Popup UI
 *
 * Design: dark, high-end tech aesthetic (Linear/Raycast-inspired).
 * Monospace numerals, tight spacing, subtle borders, no decoration.
 */

import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

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

// --- Design Tokens ---

const C = {
    bg: "#09090b",
    surface: "#111113",
    surfaceHover: "#18181b",
    border: "rgba(255,255,255,.06)",
    borderLight: "rgba(255,255,255,.04)",
    text: "#e4e4e7",
    textSecondary: "#71717a",
    textTertiary: "#3f3f46",
    accent: "#10b981",       // emerald green
    accentDim: "rgba(16,185,129,.12)",
    danger: "#ef4444",
    dangerDim: "rgba(239,68,68,.1)",
    warn: "#f59e0b",
    warnDim: "rgba(245,158,11,.1)",
    blue: "#3b82f6",
    blueDim: "rgba(59,130,246,.1)",
    font: "-apple-system, BlinkMacSystemFont, 'Inter', 'SF Pro Text', system-ui, sans-serif",
    mono: "'SF Mono', 'Fira Code', 'JetBrains Mono', ui-monospace, monospace",
    radius: 10,
    radiusSm: 8,
};

const STATE_COLORS: Record<string, string> = {
    FLOW: C.accent,
    HYPER: C.danger,
    HYPO: C.blue,
    RECOVERY: C.warn,
};

const STATE_LABELS: Record<string, string> = {
    FLOW: "Focused",
    HYPER: "Elevated",
    HYPO: "Low",
    RECOVERY: "Recovering",
};

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
    const stateColor = STATE_COLORS[stateStr] || C.textTertiary;
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
                    <div style={S.statusRow}>
                        <div style={{ ...S.statusDot, background: stateColor, boxShadow: `0 0 6px ${stateColor}40` }} />
                        <span style={{ ...S.statusText, color: stateColor }}>{stateLabel}</span>
                    </div>
                )}
            </div>

            {/* Launch / Camera */}
            <button
                style={{
                    ...S.primaryBtn,
                    marginBottom: 10,
                    background: launchError ? C.dangerDim
                        : launching ? C.surfaceHover
                        : connected ? C.surface : C.accent,
                    color: launchError ? C.danger
                        : launching ? C.textSecondary
                        : connected ? C.text : "#fff",
                    border: connected && !launchError ? `1px solid ${C.border}` : "none",
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
                            : "Launch Cortex"}
            </button>

            {/* Morning Briefing */}
            {briefing && (
                <div style={{ ...S.card, borderColor: "rgba(16,185,129,.15)" }}>
                    <div style={S.sectionHead}>Where you left off</div>
                    <div style={{ fontSize: 12, color: C.text, lineHeight: 1.5, marginBottom: 8 }}>{briefing.summary}</div>
                    {briefing.action_items.length > 0 && (
                        <div style={{ marginBottom: 6 }}>
                            {briefing.action_items.map((item, i) => (
                                <div key={i} style={{ ...S.tabRow, padding: "2px 0" }}>
                                    <span style={{ ...S.tabXMark, color: C.accent }}>{i + 1}.</span>
                                    <span style={{ fontSize: 11, color: C.textSecondary }}>{item}</span>
                                </div>
                            ))}
                        </div>
                    )}
                    <button
                        style={{ ...S.primaryBtn, fontSize: 11, padding: "6px 0" }}
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

            {/* Active Focus */}
            {focus && (
                <div style={S.card}>
                    <div style={S.focusHeader}>
                        <div>
                            <div style={S.focusGoal}>{focus.goal}</div>
                            <div style={S.muted}>{elapsedMin}m elapsed</div>
                        </div>
                        <button style={S.endBtn} onClick={handleStopFocus}>End</button>
                    </div>

                    <div style={S.bigRow}>
                        <span style={S.bigNum}>{focusMin}</span>
                        <div>
                            <div style={S.bigLabel}>min focused</div>
                            <div style={S.muted}>{focus.focusPct}%</div>
                        </div>
                    </div>

                    <div style={S.trackOuter}>
                        <div style={{
                            ...S.trackFill,
                            width: `${Math.min(focus.focusPct, 100)}%`,
                            background: focus.focusPct >= 70 ? C.accent :
                                focus.focusPct >= 40 ? C.warn : C.danger,
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
                        <Metric label="bpm" value={hr ? String(Math.round(hr)) : "--"} />
                        <div style={S.metricDiv} />
                        <Metric label="hrv" value={hrv ? String(Math.round(hrv)) : "--"} />
                        <div style={S.metricDiv} />
                        <Metric label="blinks" value={blink ? String(Math.round(blink)) : "--"} />
                    </div>
                </div>
            )}

            {/* Intervention */}
            {hasIntervention && (
                <div style={{ ...S.card, borderColor: "rgba(255,255,255,.08)" }}>
                    {closeTabs.length > 0 && (
                        <div style={{ marginBottom: 12 }}>
                            <div style={S.sectionHead}>Closing {closeTabs.length} tab{closeTabs.length !== 1 ? "s" : ""}</div>
                            {closeTabs.map((t, i) => {
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
                                            <div style={{ fontSize: 10, color: C.textTertiary, marginLeft: 22, lineHeight: 1.3 }}>{reason}</div>
                                        )}
                                    </div>
                                );
                            })}
                            {keepTabs.length > 0 && (
                                <div style={S.keepLine}>Keeping <span style={{ color: C.accent }}>{keepTabs.length}</span> you need</div>
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

                    {realCausal && (
                        <div style={{ fontSize: 11, color: C.textTertiary, lineHeight: 1.5, marginBottom: 10, fontStyle: "italic" }}>
                            {realCausal}
                        </div>
                    )}

                    {!tabRecs && !realErrAnalysis && rec.length > 0 && (
                        <div style={{ marginBottom: 10 }}>
                            {rec.map((a, i) => (
                                <div key={i} style={S.tabRow}>
                                    <span style={{ ...S.tabXMark, color: C.textSecondary }}>{"\u2022"}</span>
                                    <span style={{ ...S.tabName, color: C.text }}>{String(a.label || "")}</span>
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
                                            // Show "Done" briefly, then clear the intervention card
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
                            background: tabCloseDisabled ? C.borderLight : C.accent,
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

function Metric({ label, value, unit }: { label: string; value: string; unit?: string }): React.ReactElement {
    return (
        <div style={S.metric}>
            <span style={S.metricVal}>
                {value}
                {unit && <span style={S.metricUnit}>{unit}</span>}
            </span>
            <span style={S.metricLabel}>{label}</span>
        </div>
    );
}

// --- Styles ---

const S: Record<string, React.CSSProperties> = {
    root: {
        width: 340,
        padding: 14,
        fontFamily: C.font,
        fontSize: 13,
        color: C.text,
        background: C.bg,
    },

    // Alert
    alertBox: {
        padding: "10px 12px",
        borderRadius: C.radiusSm,
        background: C.surface,
        border: `1px solid ${C.border}`,
        marginBottom: 10,
    },
    alertTitle: { fontSize: 12, fontWeight: 600, marginBottom: 2, color: C.text },
    alertBody: { fontSize: 11, color: C.textSecondary, lineHeight: 1.5 },

    // Header
    header: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 14,
        paddingBottom: 12,
        borderBottom: `1px solid ${C.borderLight}`,
    },
    logoRow: { display: "flex", alignItems: "center", gap: 7 },
    logoMark: {
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: `linear-gradient(135deg, ${C.accent}, #059669)`,
        boxShadow: `0 0 8px ${C.accent}30`,
    },
    logoText: {
        fontSize: 13,
        fontWeight: 600,
        letterSpacing: -0.3,
        color: C.text,
    },
    connectBtn: {
        padding: "4px 12px",
        border: `1px solid ${C.border}`,
        borderRadius: 6,
        background: "transparent",
        color: C.textSecondary,
        cursor: "pointer",
        fontSize: 11,
        fontWeight: 500,
        fontFamily: C.font,
    },
    statusRow: { display: "flex", alignItems: "center", gap: 6 },
    statusDot: { width: 6, height: 6, borderRadius: "50%", transition: "all 1s" },
    statusText: { fontSize: 11, fontWeight: 600, fontFamily: C.mono, letterSpacing: 0.5, transition: "color 1s" },

    // Sections
    section: { marginBottom: 10 },
    input: {
        width: "100%",
        padding: "9px 11px",
        border: `1px solid ${C.border}`,
        borderRadius: C.radiusSm,
        background: C.surface,
        color: C.text,
        fontSize: 12,
        marginBottom: 8,
        outline: "none",
        boxSizing: "border-box" as const,
        fontFamily: C.font,
    },
    primaryBtn: {
        width: "100%",
        padding: "9px 0",
        border: "none",
        borderRadius: C.radiusSm,
        background: C.text,
        color: C.bg,
        fontSize: 12,
        fontWeight: 600,
        cursor: "pointer",
        letterSpacing: -0.1,
        fontFamily: C.font,
        transition: "opacity .15s",
    },
    doneBtnStyle: {
        background: C.accent,
        color: "#fff",
        cursor: "default",
        pointerEvents: "none" as const,
    },

    // Card
    card: {
        background: C.surface,
        borderRadius: C.radius,
        padding: 12,
        marginBottom: 8,
        border: `1px solid ${C.borderLight}`,
    },

    // Focus
    focusHeader: {
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        marginBottom: 14,
    },
    focusGoal: { fontSize: 12, fontWeight: 600, color: C.text },
    muted: { fontSize: 10, color: C.textSecondary, marginTop: 2, fontFamily: C.mono },
    endBtn: {
        padding: "3px 10px",
        border: `1px solid ${C.dangerDim}`,
        borderRadius: 6,
        background: C.dangerDim,
        color: C.danger,
        cursor: "pointer",
        fontSize: 10,
        fontWeight: 600,
        fontFamily: C.font,
    },
    bigRow: { display: "flex", alignItems: "baseline", gap: 8, marginBottom: 10 },
    bigNum: {
        fontSize: 36,
        fontWeight: 200,
        color: C.accent,
        letterSpacing: -2,
        lineHeight: 1,
        fontFamily: C.mono,
    },
    bigLabel: { fontSize: 12, color: C.textSecondary },

    // Progress track
    trackOuter: {
        height: 2,
        borderRadius: 1,
        background: C.borderLight,
        marginBottom: 14,
        overflow: "hidden",
    },
    trackFill: {
        height: "100%",
        borderRadius: 1,
        transition: "width 1s ease, background 2s ease",
    },

    // Metrics row
    metricsRow: { display: "flex", alignItems: "center", justifyContent: "space-around" },
    metric: { display: "flex", flexDirection: "column" as const, alignItems: "center", gap: 2 },
    metricVal: { fontSize: 15, fontWeight: 400, color: C.text, fontFamily: C.mono },
    metricUnit: { fontSize: 10, color: C.textSecondary, marginLeft: 1 },
    metricLabel: { fontSize: 9, color: C.textTertiary, letterSpacing: 0.8, fontFamily: C.mono },
    metricDiv: { width: 1, height: 16, background: C.borderLight },

    // Intervention
    sectionHead: { fontSize: 11, fontWeight: 500, color: C.textSecondary, marginBottom: 8 },
    tabRow: { display: "flex", alignItems: "center", gap: 8, padding: "3px 0" },
    tabXMark: { color: C.danger, fontSize: 13, fontWeight: 500, width: 14, textAlign: "center" as const, flexShrink: 0, fontFamily: C.mono },
    tabName: {
        fontSize: 12, color: C.textSecondary,
        whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis" as const,
    },
    keepLine: { fontSize: 11, color: C.textTertiary, marginTop: 6 },

    // Error
    errBox: {
        padding: "10px 12px",
        background: C.dangerDim,
        borderRadius: C.radiusSm,
        border: `1px solid rgba(239,68,68,.08)`,
        marginBottom: 12,
    },
    errHead: { fontSize: 10, fontWeight: 600, color: C.danger, marginBottom: 4, fontFamily: C.mono, letterSpacing: 0.5 },
    errBody: { fontSize: 12, color: C.text, lineHeight: 1.5 },
    errCode: {
        fontSize: 11, color: C.textSecondary, marginTop: 8, fontFamily: C.mono,
        padding: "8px 10px", background: "rgba(0,0,0,.3)", borderRadius: 6, lineHeight: 1.5,
        whiteSpace: "pre-wrap" as const, border: "none", margin: 0,
    },

    // Undo
    undoRow: {
        display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
        marginTop: 6, fontSize: 11, color: C.textTertiary,
    },
    undoLink: {
        background: "none", border: "none", color: C.blue, fontSize: 11,
        fontWeight: 500, cursor: "pointer", padding: 0, fontFamily: C.font,
    },

    // Daily stats
    dailyGrid: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 4 },
    dailyItem: { display: "flex", flexDirection: "column" as const, alignItems: "center", gap: 2 },

    // Toggle
    toggleRow: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
    },
    toggleLabel: { fontSize: 12, fontWeight: 500, color: C.text },
    toggleDesc: { fontSize: 10, color: C.textTertiary, marginTop: 2 },
    toggleTrack: {
        position: "relative" as const,
        width: 36,
        height: 20,
        borderRadius: 10,
        border: "none",
        cursor: "pointer",
        padding: 0,
        flexShrink: 0,
        transition: "background .2s",
    },
    toggleThumb: {
        position: "absolute" as const,
        top: 2,
        left: 2,
        width: 16,
        height: 16,
        borderRadius: "50%",
        background: "#fff",
        transition: "transform .2s",
    },
};

// --- Mount ---

const root = document.getElementById("root");
if (root) {
    createRoot(root).render(<CortexPopup />);
}

export default CortexPopup;
