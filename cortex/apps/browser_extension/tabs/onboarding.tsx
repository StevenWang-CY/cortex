/**
 * Cortex Chrome Extension — Onboarding Page
 *
 * A minimal 3-step guide shown on first install:
 *   1. Launch desktop app (daemon auto-starts)
 *   2. Permissions explained
 *   3. Calibrate baseline
 */

import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "../page-reset.css";
import { CX, CX_KEYFRAMES } from "../design-tokens";
import { getLastRuntimeError } from "../lib/chrome-runtime";

const STEPS = [
    {
        // Phase 4d Task E: the desktop .app launches the daemon in-process —
        // no terminal command required. Onboarding copy was stuck on the
        // dev-mode instructions which confused first-run users on the DMG
        // install path.
        title: "Launch the Cortex desktop app",
        body: "Launch the Cortex desktop app — the daemon starts automatically. The extension will connect on its own as soon as the app is running.",
        hint: "Don't have the desktop app yet? Install Cortex.dmg from your download or grab it from cortex.so.",
    },
    {
        title: "Permissions explained",
        body: "Cortex needs a few browser permissions to help you focus:",
        items: [
            ["Tabs & Tab Groups", "Read tab titles and origins to understand your workspace. Group or collapse tabs only after you approve the exact proposal."],
            ["Storage", "Persist your focus sessions, daily stats, and preferences locally on your machine."],
            ["Webcam (optional)", "Estimate blink timing and a camera-relative head/neck position proxy. Cortex does not measure shoulders or diagnose posture; video is processed locally and is not stored."],
        ],
        hint: "Camera frames stay on your machine and are never stored. If you enable an external LLM, selected workspace context is sent to that provider under its retention policy.",
    },
    {
        title: "Calibrate your baseline",
        body: "Cortex learns your personal focus patterns over the first few sessions. For the best experience:",
        items: [
            ["Start a focus session", "Click the Cortex icon, enter what you are working on, and press Return."],
            ["Work normally", "Cortex builds local behavior baselines. Optional camera signals remain experimental and do not trigger support actions."],
            ["Review proposals", "When sustained behavior suggests support may help, Cortex can offer workspace proposals. Break reminders are opt-in and time-based."],
        ],
        hint: "Your local baseline improves as valid sessions accumulate. Cortex stays quiet when evidence is insufficient.",
    },
];

function Onboarding(): React.ReactElement {
    const [step, setStep] = useState(0);
    const current = STEPS[step];
    const isLast = step === STEPS.length - 1;

    // Phase 4d Task F: poll the background script for daemon
    // connectivity on the first step so the user can't blindly click
    // "Next" against a dead daemon. ``null`` = not yet probed,
    // ``true``/``false`` mirror background's ``connected`` flag.
    const [daemonConnected, setDaemonConnected] = useState<boolean | null>(null);
    const [skippedOffline, setSkippedOffline] = useState(false);
    const [offlineWarning, setOfflineWarning] = useState(false);

    useEffect(() => {
        // Only poll on the daemon step — re-polling later steps adds
        // no signal and noise up the runtime.lastError handling.
        if (step !== 0) return;
        let cancelled = false;
        const probe = () => {
            try {
                chrome.runtime.sendMessage({ type: "GET_STATE" }, (resp) => {
                    // F10 (Phase-4 audit): read lastError *before*
                    // dereferencing ``resp``. The MV3 runtime sets
                    // lastError when the background SW is unreachable,
                    // and reading the response in that case throws a
                    // confusing "Unchecked runtime.lastError" warning.
                    // Centralised reader from lib/chrome-runtime.ts.
                    const lastErr = getLastRuntimeError();
                    if (cancelled) return;
                    if (lastErr) {
                        if (typeof console !== "undefined") {
                            console.warn(
                                "[cortex.onboarding] GET_STATE lastError:",
                                lastErr.message,
                            );
                        }
                        setDaemonConnected(false);
                        return;
                    }
                    if (!resp) {
                        setDaemonConnected(false);
                        return;
                    }
                    const r = resp as { connected?: boolean };
                    setDaemonConnected(Boolean(r.connected));
                });
            } catch {
                if (!cancelled) setDaemonConnected(false);
            }
        };
        probe();
        const handle = setInterval(probe, 2000);
        return () => {
            cancelled = true;
            clearInterval(handle);
        };
    }, [step]);

    const isDaemonStep = step === 0;
    const nextBlocked =
        isDaemonStep
        && daemonConnected !== true
        && !skippedOffline;

    return (
        <div style={S.page}>
            <style>{`
                html, body, #__plasmo {
                    margin: 0;
                    padding: 0;
                    background: ${CX.bg};
                    min-height: 100%;
                }
                * { box-sizing: border-box; }
                ${CX_KEYFRAMES}
                .cortex-onboarding-card { padding: 48px; }
                button:not(:disabled) {
                    transition: transform ${CX.durationMicro} ${CX.easeOut},
                        background-color ${CX.durationFast} ${CX.easeOut},
                        border-color ${CX.durationFast} ${CX.easeOut},
                        color ${CX.durationFast} ${CX.easeOut},
                        opacity ${CX.durationFast} ${CX.easeOut};
                }
                button:active:not(:disabled) { transform: scale(.97); }
                @media (max-width: 560px) {
                    .cortex-onboarding-card { padding: 28px 24px; border-radius: 16px !important; }
                }
                @media (prefers-reduced-motion: reduce) {
                    button:not(:disabled) { transition-property: background-color, border-color, color, opacity; }
                    button:active:not(:disabled) { transform: none; }
                }
            `}</style>
            <main className="cortex-onboarding-card" style={S.container}>
                {/* Progress */}
                <div
                    style={S.progressRow}
                    role="progressbar"
                    aria-label="Onboarding progress"
                    aria-valuemin={1}
                    aria-valuemax={STEPS.length}
                    aria-valuenow={step + 1}
                >
                    {STEPS.map((_, i) => (
                        <div
                            key={i}
                            aria-hidden="true"
                            style={{
                                ...S.progressDot,
                                background: i <= step ? CX.accent : CX.border,
                            }}
                        />
                    ))}
                </div>

                {/* Step indicator */}
                <div style={S.stepLabel}>Step {step + 1} of {STEPS.length}</div>

                {/* Title */}
                <h1 style={S.title}>{current.title}</h1>

                {/* Body */}
                <p style={S.body}>{current.body}</p>

                {/* Item list */}
                {current.items && (
                    <ol style={S.itemList}>
                        {current.items.map(([label, desc], i) => (
                            <li key={i} style={S.item}>
                                <div style={S.itemNum}>{i + 1}</div>
                                <div>
                                    <div style={S.itemLabel}>{label}</div>
                                    <div style={S.itemDesc}>{desc}</div>
                                </div>
                            </li>
                        ))}
                    </ol>
                )}

                {/* Phase 4d Task F: daemon connectivity status — only on
                    the launch step. Renders a coloured pill plus a
                    "Skip offline" escape hatch so users without the
                    desktop app installed can still finish onboarding. */}
                {isDaemonStep && (
                    <div
                        data-testid="daemon-health-status"
                        role="status"
                        aria-live="polite"
                        style={{
                            ...S.hint,
                            marginBottom: 16,
                            fontStyle: "normal",
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            color: CX.text,
                        }}
                    >
                        <span
                            aria-hidden="true"
                            style={{
                                display: "inline-block",
                                width: 8,
                                height: 8,
                                borderRadius: "50%",
                                background:
                                    daemonConnected === true
                                        ? "#4CAF50"
                                        : daemonConnected === false
                                            ? "#E47A6E"
                                            : CX.textTertiary,
                            }}
                        />
                        <span data-testid="daemon-health-label">
                            {daemonConnected === true
                                ? "Connected to daemon"
                                : daemonConnected === false
                                    ? "Daemon not detected — launch the desktop app"
                                    : "Checking daemon…"}
                        </span>
                        {daemonConnected !== true && !skippedOffline && (
                            <button
                                data-testid="onboarding-skip-offline"
                                onClick={() => {
                                    setSkippedOffline(true);
                                    setOfflineWarning(true);
                                    setTimeout(
                                        () => setOfflineWarning(false),
                                        4000,
                                    );
                                }}
                                style={{
                                    background: "transparent",
                                    border: "none",
                                    color: CX.accentText,
                                    fontSize: 12,
                                    cursor: "pointer",
                                    textDecoration: "underline",
                                    padding: 0,
                                    marginLeft: "auto",
                                    fontFamily: CX.font,
                                }}
                                aria-label="Skip — continue offline"
                            >
                                Skip — continue offline
                            </button>
                        )}
                    </div>
                )}

                {offlineWarning && (
                    <div
                        role="status"
                        data-testid="onboarding-offline-toast"
                        style={{
                            ...S.hint,
                            marginBottom: 16,
                            color: CX.danger,
                            fontStyle: "normal",
                        }}
                    >
                        Continuing without daemon — features will be limited
                        until you launch the desktop app.
                    </div>
                )}

                {/* Hint */}
                {current.hint && (
                    <div style={S.hint}>{current.hint}</div>
                )}

                {/* Navigation */}
                <div style={S.navRow}>
                    {step > 0 && (
                        <button
                            style={S.backBtn}
                            onClick={() => setStep(step - 1)}
                        >
                            Back
                        </button>
                    )}
                    <div style={{ flex: 1 }} />
                    <button
                        data-testid="onboarding-next-btn"
                        disabled={nextBlocked}
                        aria-disabled={nextBlocked}
                        style={{
                            ...S.nextBtn,
                            opacity: nextBlocked ? 0.5 : 1,
                            cursor: nextBlocked ? "not-allowed" : "pointer",
                        }}
                        onClick={() => {
                            if (nextBlocked) return;
                            if (isLast) {
                                window.close();
                            } else {
                                setStep(step + 1);
                            }
                        }}
                    >
                        {isLast ? "Get started" : "Next"}
                    </button>
                </div>
            </main>
        </div>
    );
}

// --- Styles ---

const S: Record<string, React.CSSProperties> = {
    page: {
        minHeight: "100vh",
        display: "flex",
        alignItems: "stretch",
        justifyContent: "flex-start",
        overflowY: "auto",
        background: CX.bg,
        fontFamily: CX.font,
        color: CX.text,
        padding: 24,
    },
    container: {
        maxWidth: 520,
        width: "100%",
        background: CX.surface,
        borderRadius: 24,
        border: `1px solid ${CX.border}`,
        margin: "auto",
    },
    progressRow: {
        display: "flex",
        gap: 8,
        marginBottom: 24,
    },
    progressDot: {
        flex: 1,
        height: 3,
        borderRadius: 2,
        transition: `background-color ${CX.durationNormal} ${CX.easeOut}`,
    },
    stepLabel: {
        fontSize: 11,
        color: CX.textTertiary,
        letterSpacing: "0.04em",
        textTransform: "uppercase" as const,
        fontFamily: CX.mono,
        marginBottom: 8,
    },
    title: {
        fontSize: 22,
        fontWeight: 600,
        letterSpacing: -0.5,
        color: CX.text,
        margin: "0 0 12px 0",
        lineHeight: 1.3,
    },
    body: {
        fontSize: 14,
        color: CX.textSecondary,
        lineHeight: 1.6,
        margin: "0 0 20px 0",
    },
    codeBlock: {
        background: CX.bg,
        border: `1px solid ${CX.border}`,
        borderRadius: CX.radiusLg,
        padding: "14px 18px",
        fontFamily: CX.mono,
        fontSize: 14,
        color: CX.accentText,
        marginBottom: 16,
        overflow: "auto" as const,
    },
    itemList: {
        display: "flex",
        flexDirection: "column" as const,
        gap: 16,
        margin: "0 0 20px",
        padding: 0,
        listStyle: "none",
    },
    item: {
        display: "flex",
        gap: 14,
        alignItems: "flex-start",
    },
    itemNum: {
        width: 24,
        height: 24,
        borderRadius: "50%",
        background: CX.accentDim,
        color: CX.accentText,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 12,
        fontWeight: 600,
        fontFamily: CX.mono,
        flexShrink: 0,
    },
    itemLabel: {
        fontSize: 13,
        fontWeight: 600,
        color: CX.text,
        marginBottom: 2,
    },
    itemDesc: {
        fontSize: 12,
        color: CX.textSecondary,
        lineHeight: 1.5,
    },
    hint: {
        fontSize: 12,
        color: CX.textTertiary,
        fontStyle: "italic",
        marginBottom: 24,
        lineHeight: 1.5,
    },
    navRow: {
        display: "flex",
        alignItems: "center",
        gap: 12,
        marginTop: 8,
    },
    backBtn: {
        padding: "10px 20px",
        border: `1px solid ${CX.border}`,
        borderRadius: CX.radiusLg,
        background: "transparent",
        color: CX.textSecondary,
        fontSize: 13,
        fontWeight: 500,
        cursor: "pointer",
        fontFamily: CX.font,
    },
    nextBtn: {
        padding: "10px 28px",
        border: "none",
        borderRadius: CX.radiusLg,
        background: CX.accent,
        color: CX.light.label_primary,
        fontSize: 13,
        fontWeight: 500,
        cursor: "pointer",
        fontFamily: CX.font,
        letterSpacing: 0.3,
    },
};

// --- Mount ---
//
// Audit-2 fix: fall back to Plasmo's ``#__plasmo`` wrapper when
// ``#root`` isn't present (which is the production HTML emitted by
// Plasmo's codegen). Without this, the onboarding tab loaded with an
// empty body — the React tree never mounted and first-run users saw
// nothing.

const root =
    document.getElementById("root") ?? document.getElementById("__plasmo");
if (root) {
    createRoot(root).render(<Onboarding />);
}

export default Onboarding;
