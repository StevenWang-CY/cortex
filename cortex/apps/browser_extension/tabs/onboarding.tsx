/**
 * Cortex Chrome Extension — Onboarding Page
 *
 * A minimal 3-step guide shown on first install:
 *   1. Start daemon
 *   2. Permissions explained
 *   3. Calibrate baseline
 */

import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { CX, CX_KEYFRAMES } from "../design-tokens";

const STEPS = [
    {
        title: "Start the Cortex daemon",
        body: "Cortex runs a local daemon that processes your biometric signals. Open a terminal and run:",
        code: "cortex-dev",
        hint: "The extension will automatically connect once the daemon is running.",
    },
    {
        title: "Permissions explained",
        body: "Cortex needs a few browser permissions to help you focus:",
        items: [
            ["Tabs & Tab Groups", "Read tab titles and URLs to understand your workspace context. Group and collapse distracting tabs during interventions."],
            ["Storage", "Persist your focus sessions, daily stats, and preferences locally on your machine."],
            ["Webcam (optional)", "Detect blink rate and posture via your camera. All processing is local — no video is stored or transmitted."],
        ],
        hint: "All data stays on your machine. Nothing is sent to external servers.",
    },
    {
        title: "Calibrate your baseline",
        body: "Cortex learns your personal focus patterns over the first few sessions. For the best experience:",
        items: [
            ["Start a focus session", "Click the Cortex icon in your toolbar, enter what you are working on, and press Start Session."],
            ["Work normally", "Cortex observes your heart rate, blink rate, and browsing patterns to build your baseline."],
            ["Review interventions", "When Cortex detects overwhelm, it will suggest tab cleanups and breaks. Accept or dismiss to teach it your preferences."],
        ],
        hint: "After 2-3 sessions, Cortex adapts to your rhythm.",
    },
];

function Onboarding(): React.ReactElement {
    const [step, setStep] = useState(0);
    const current = STEPS[step];
    const isLast = step === STEPS.length - 1;

    return (
        <div style={S.page}>
            <style>{CX_KEYFRAMES}</style>
            <div style={S.container}>
                {/* Progress */}
                <div style={S.progressRow}>
                    {STEPS.map((_, i) => (
                        <div
                            key={i}
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

                {/* Code block */}
                {current.code && (
                    <pre style={S.codeBlock}>
                        <code>{current.code}</code>
                    </pre>
                )}

                {/* Item list */}
                {current.items && (
                    <div style={S.itemList}>
                        {current.items.map(([label, desc], i) => (
                            <div key={i} style={S.item}>
                                <div style={S.itemNum}>{i + 1}</div>
                                <div>
                                    <div style={S.itemLabel}>{label}</div>
                                    <div style={S.itemDesc}>{desc}</div>
                                </div>
                            </div>
                        ))}
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
                        style={S.nextBtn}
                        onClick={() => {
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
            </div>
        </div>
    );
}

// --- Styles ---

const S: Record<string, React.CSSProperties> = {
    page: {
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: CX.bg,
        fontFamily: CX.font,
        color: CX.text,
        padding: 24,
    },
    container: {
        maxWidth: 520,
        width: "100%",
        background: CX.surface,
        borderRadius: CX.radiusXl,
        border: `1px solid ${CX.borderDefault}`,
        padding: 40,
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
        transition: "background 0.3s ease",
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
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusMd,
        padding: "14px 18px",
        fontFamily: CX.mono,
        fontSize: 14,
        color: CX.accent,
        marginBottom: 16,
        overflow: "auto" as const,
    },
    itemList: {
        display: "flex",
        flexDirection: "column" as const,
        gap: 16,
        marginBottom: 20,
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
        color: CX.accent,
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
        border: `1px solid ${CX.borderDefault}`,
        borderRadius: CX.radiusMd,
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
        borderRadius: CX.radiusMd,
        background: CX.accent,
        color: CX.textInverse,
        fontSize: 13,
        fontWeight: 500,
        cursor: "pointer",
        fontFamily: CX.font,
        letterSpacing: 0.3,
    },
};

// --- Mount ---

const root = document.getElementById("root");
if (root) {
    createRoot(root).render(<Onboarding />);
}

export default Onboarding;
