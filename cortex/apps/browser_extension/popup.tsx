/**
 * Cortex Chrome Extension — Popup UI
 *
 * Status dashboard shown when the extension icon is clicked:
 * - Connection status (connected/disconnected to daemon)
 * - Current cognitive state (FLOW/HYPO/HYPER/RECOVERY) + confidence
 * - Estimated heart rate (from signal quality)
 * - Sensitivity slider (1-5 scale)
 * - Quiet mode toggle
 * - Active intervention display
 */

import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

// --- Types ---

interface CortexState {
    state: string;
    confidence: number;
    scores: Record<string, number>;
    signal_quality: Record<string, number>;
    dwell_seconds: number;
}

// --- State Colors ---

const STATE_COLORS: Record<string, string> = {
    FLOW: "#4CAF50",
    HYPER: "#F44336",
    HYPO: "#6495ED",
    RECOVERY: "#FFC107",
};

// --- Main Component ---

function CortexPopup(): React.ReactElement {
    const [connected, setConnected] = useState(false);
    const [state, setState] = useState<CortexState | null>(null);
    const [intervention, setIntervention] = useState<Record<
        string,
        unknown
    > | null>(null);
    const [sensitivity, setSensitivity] = useState(3);
    const [quietMode, setQuietMode] = useState(false);

    // Load saved settings
    useEffect(() => {
        chrome.storage.local.get(
            ["cortex_sensitivity", "cortex_quiet_mode"],
            (result) => {
                if (result.cortex_sensitivity !== undefined) {
                    setSensitivity(result.cortex_sensitivity as number);
                }
                if (result.cortex_quiet_mode !== undefined) {
                    setQuietMode(result.cortex_quiet_mode as boolean);
                }
            },
        );
    }, []);

    // Get current state from background
    useEffect(() => {
        chrome.runtime.sendMessage(
            { type: "GET_STATE" },
            (response: {
                connected: boolean;
                state: CortexState | null;
                intervention: Record<string, unknown> | null;
            }) => {
                if (response) {
                    setConnected(response.connected);
                    setState(response.state);
                    setIntervention(response.intervention);
                }
            },
        );
    }, []);

    // Listen for updates from background
    useEffect(() => {
        const listener = (message: Record<string, unknown>) => {
            switch (message.type) {
                case "CONNECTION_CHANGED":
                    setConnected(message.connected as boolean);
                    break;
                case "STATE_UPDATE":
                    setState(message.payload as CortexState);
                    break;
                case "INTERVENTION_TRIGGER":
                    setIntervention(
                        message.payload as Record<string, unknown>,
                    );
                    break;
            }
        };
        chrome.runtime.onMessage.addListener(listener);
        return () => chrome.runtime.onMessage.removeListener(listener);
    }, []);

    // Handlers
    const handleConnect = useCallback(() => {
        chrome.runtime.sendMessage({ type: "CONNECT" });
    }, []);

    const handleDisconnect = useCallback(() => {
        chrome.runtime.sendMessage({ type: "DISCONNECT" });
    }, []);

    const handleSensitivityChange = useCallback(
        (e: React.ChangeEvent<HTMLInputElement>) => {
            const value = parseInt(e.target.value, 10);
            setSensitivity(value);
            chrome.storage.local.set({ cortex_sensitivity: value });
        },
        [],
    );

    const handleQuietModeChange = useCallback(
        (e: React.ChangeEvent<HTMLInputElement>) => {
            const checked = e.target.checked;
            setQuietMode(checked);
            chrome.storage.local.set({ cortex_quiet_mode: checked });
        },
        [],
    );

    const handleDismiss = useCallback(() => {
        if (intervention) {
            chrome.runtime.sendMessage({
                type: "USER_ACTION",
                action: "dismissed",
                intervention_id: intervention.intervention_id,
            });
            setIntervention(null);
        }
    }, [intervention]);

    // Derived values
    const stateStr = state?.state ?? "—";
    const stateColor = STATE_COLORS[stateStr] ?? "#888";
    const confPct = state ? Math.round(state.confidence * 100) : 0;
    const signalQuality = state?.signal_quality?.overall ?? 0;
    const qualityPct = Math.round(signalQuality * 100);

    return (
        <div style={styles.container}>
            {/* Header */}
            <div style={styles.header}>
                <span style={styles.title}>Cortex</span>
                <span
                    style={{
                        ...styles.connDot,
                        backgroundColor: connected ? "#4CAF50" : "#888",
                    }}
                />
            </div>

            {/* Connection */}
            <div style={styles.row}>
                <span style={styles.label}>
                    {connected ? "Connected" : "Disconnected"}
                </span>
                <button
                    style={styles.btn}
                    onClick={connected ? handleDisconnect : handleConnect}
                >
                    {connected ? "Disconnect" : "Connect"}
                </button>
            </div>

            {/* State */}
            <div style={styles.stateCard}>
                <div style={styles.stateRow}>
                    <div
                        style={{
                            ...styles.stateDot,
                            backgroundColor: stateColor,
                        }}
                    />
                    <span style={styles.stateLabel}>{stateStr}</span>
                    <span style={styles.confLabel}>{confPct}%</span>
                </div>
                <div style={styles.qualityRow}>
                    <span style={styles.qualityLabel}>Signal Quality</span>
                    <div style={styles.qualityBarOuter}>
                        <div
                            style={{
                                ...styles.qualityBarInner,
                                width: `${qualityPct}%`,
                                backgroundColor:
                                    qualityPct >= 70
                                        ? "#4CAF50"
                                        : qualityPct >= 40
                                          ? "#FFC107"
                                          : "#F44336",
                            }}
                        />
                    </div>
                    <span style={styles.qualityPct}>{qualityPct}%</span>
                </div>
            </div>

            {/* Sensitivity */}
            <div style={styles.settingRow}>
                <label style={styles.settingLabel}>
                    Sensitivity: {sensitivity}
                </label>
                <input
                    type="range"
                    min="1"
                    max="5"
                    value={sensitivity}
                    onChange={handleSensitivityChange}
                    style={styles.slider}
                />
            </div>

            {/* Quiet Mode */}
            <div style={styles.settingRow}>
                <label style={styles.settingLabel}>Quiet Mode</label>
                <input
                    type="checkbox"
                    checked={quietMode}
                    onChange={handleQuietModeChange}
                    style={styles.checkbox}
                />
            </div>

            {/* Active Intervention */}
            {intervention && (
                <div style={styles.interventionCard}>
                    <div style={styles.interventionHeadline}>
                        {intervention.headline as string}
                    </div>
                    <div style={styles.interventionSummary}>
                        {intervention.situation_summary as string}
                    </div>
                    <button style={styles.dismissBtn} onClick={handleDismiss}>
                        Dismiss
                    </button>
                </div>
            )}
        </div>
    );
}

// --- Styles ---

const styles: Record<string, React.CSSProperties> = {
    container: {
        width: 320,
        padding: 16,
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        fontSize: 13,
        color: "#e6f0ff",
        background: "#0e1525",
    },
    header: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 12,
    },
    title: {
        fontSize: 16,
        fontWeight: 700,
    },
    connDot: {
        width: 8,
        height: 8,
        borderRadius: "50%",
        display: "inline-block",
    },
    row: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 12,
    },
    label: {
        color: "#a0b4d2",
        fontSize: 12,
    },
    btn: {
        padding: "4px 12px",
        border: "1px solid rgba(255,255,255,0.15)",
        borderRadius: 4,
        background: "rgba(255,255,255,0.06)",
        color: "#e6f0ff",
        cursor: "pointer",
        fontSize: 12,
    },
    stateCard: {
        background: "#1a2540",
        borderRadius: 8,
        padding: 12,
        marginBottom: 12,
    },
    stateRow: {
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginBottom: 8,
    },
    stateDot: {
        width: 12,
        height: 12,
        borderRadius: "50%",
    },
    stateLabel: {
        fontWeight: 600,
        flex: 1,
    },
    confLabel: {
        color: "#a0b4d2",
        fontSize: 12,
    },
    qualityRow: {
        display: "flex",
        alignItems: "center",
        gap: 8,
    },
    qualityLabel: {
        fontSize: 11,
        color: "#a0b4d2",
        whiteSpace: "nowrap" as const,
    },
    qualityBarOuter: {
        flex: 1,
        height: 6,
        background: "rgba(255,255,255,0.1)",
        borderRadius: 3,
        overflow: "hidden",
    },
    qualityBarInner: {
        height: "100%",
        borderRadius: 3,
        transition: "width 0.3s",
    },
    qualityPct: {
        fontSize: 11,
        color: "#a0b4d2",
        minWidth: 28,
        textAlign: "right" as const,
    },
    settingRow: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 8,
    },
    settingLabel: {
        fontSize: 12,
        color: "#a0b4d2",
    },
    slider: {
        width: 120,
        accentColor: "#64a0ff",
    },
    checkbox: {
        accentColor: "#64a0ff",
    },
    interventionCard: {
        background: "#1a2540",
        borderRadius: 8,
        padding: 12,
        marginTop: 8,
        borderLeft: "3px solid #64a0ff",
    },
    interventionHeadline: {
        fontWeight: 600,
        fontSize: 14,
        marginBottom: 4,
    },
    interventionSummary: {
        fontSize: 12,
        color: "#a0b4d2",
        marginBottom: 8,
        lineHeight: 1.4,
    },
    dismissBtn: {
        padding: "6px 16px",
        border: "1px solid rgba(255,255,255,0.15)",
        borderRadius: 4,
        background: "rgba(255,255,255,0.06)",
        color: "#e6f0ff",
        cursor: "pointer",
        fontSize: 12,
        width: "100%",
    },
};

// --- Mount ---

const root = document.getElementById("root");
if (root) {
    createRoot(root).render(<CortexPopup />);
}

export default CortexPopup;
