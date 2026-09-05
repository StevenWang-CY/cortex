/**
 * Intervention card for the popup.
 *
 * ``ApplyButton`` renders the shared apply machine (idle → pending → applied
 * | partial | failed) from the outcome the worker computed; ``RatingRow``
 * appears only after something was applied. Status-only situations
 * ("suggestions only", "manual review", "checking actions") are rendered as
 * status text, never as a disabled button pretending to be one.
 */

import React from "react";
import { CX } from "../../design-tokens";
import {
    applyStatusLabel,
    type ApplyOutcome,
    type ApplyPhase,
} from "../../lib/apply-state";
import type { ExecutionMode, MicroStep } from "../../lib/popup-view-model";
import { DANGER_TEXT, S } from "../styles";

export interface CausalSignalView {
    name: string;
    current_value: number;
    baseline_value: number | null;
    unit: string;
    delta_pct: number | null;
    samples_60s: number[];
    severity: "primary" | "secondary" | "tertiary";
}

export interface TabRecView {
    title: string;
    reason: string;
}

export interface ApplyState {
    phase: ApplyPhase;
    outcome: ApplyOutcome | null;
    /** The user undid an applied change; the proposal is consumed. */
    undone: boolean;
    undoBusy: boolean;
}

export const IDLE_APPLY_STATE: ApplyState = {
    phase: "idle",
    outcome: null,
    undone: false,
    undoBusy: false,
};

export function primaryLabel(executable: Array<Record<string, unknown>>): string {
    if (executable.length === 1) {
        const actionType = String(executable[0].action_type || "");
        if (actionType === "search_error") return "Search this error";
        if (actionType === "open_url") return "Open recommended page";
        if (actionType === "highlight_tab") return "Switch to recommended tab";
        return "Apply change";
    }
    return `Apply ${executable.length} changes`;
}

export interface ApplyButtonProps {
    apply: ApplyState;
    label: string;
    onApply: () => void;
    onUndo: () => void;
}

export function ApplyButton({
    apply,
    label,
    onApply,
    onUndo,
}: ApplyButtonProps): React.ReactElement {
    const { phase, outcome, undone, undoBusy } = apply;
    const settled = phase === "applied" || phase === "partial";
    let style: React.CSSProperties = S.primaryBtn;
    let text = label;
    let disabled = false;
    let dataPhase: string = phase;
    if (undone) {
        style = { ...S.primaryBtn, ...S.applyRestored };
        text = "Restored";
        disabled = true;
        dataPhase = "restored";
    } else if (phase === "pending") {
        style = { ...S.primaryBtn, ...S.applyPending };
        text = "Applying…";
        disabled = true;
    } else if (settled && outcome) {
        style = { ...S.primaryBtn, ...S.applyApplied };
        text = applyStatusLabel(outcome);
        disabled = true;
    } else if (phase === "failed") {
        style = { ...S.primaryBtn, ...S.applyFailed };
        text = outcome
            ? applyStatusLabel(outcome)
            : "Nothing changed — something went wrong";
        disabled = true;
    }
    return (
        <>
            <button
                data-testid="intervention-primary-action"
                data-phase={dataPhase}
                className="cortex-primary-btn"
                style={style}
                disabled={disabled}
                aria-disabled={disabled}
                aria-busy={phase === "pending" || undefined}
                onClick={() => { if (phase === "idle" && !undone) onApply(); }}
            >
                {text}
            </button>
            <div
                style={S.undoRow}
                role="status"
                aria-live="polite"
                data-testid="apply-status"
            >
                {settled && !undone && outcome && (
                    <>
                        <span>Undo stays available for a minute.</span>
                        <button
                            type="button"
                            style={S.undoLink}
                            onClick={onUndo}
                            disabled={undoBusy}
                            data-testid="intervention-undo"
                        >
                            {undoBusy ? "Undoing…" : "Undo"}
                        </button>
                    </>
                )}
                {undone && <span>Changes undone.</span>}
            </div>
        </>
    );
}

export interface RatingRowProps {
    rating: "thumbs_up" | "thumbs_down" | null;
    onRate: (rating: "thumbs_up" | "thumbs_down") => void;
}

export function RatingRow({ rating, onRate }: RatingRowProps): React.ReactElement {
    return (
        <div
            data-testid="rating-row"
            role="group"
            aria-label="Was this helpful?"
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
                onClick={() => onRate("thumbs_up")}
                style={{ ...S.ratingBtn, ...(rating === "thumbs_up" ? S.ratingBtnUp : {}) }}
            >Helpful</button>
            <button
                data-testid="rating-thumbs-down"
                aria-label="Mark unhelpful"
                aria-pressed={rating === "thumbs_down"}
                onClick={() => onRate("thumbs_down")}
                style={{ ...S.ratingBtn, ...(rating === "thumbs_down" ? S.ratingBtnDown : {}) }}
            >Not helpful</button>
        </div>
    );
}

export interface InterventionCardProps {
    interventionId: string;
    failureBanner: string | null;
    prompt: string | null;
    causalText: string;
    causalSignals: CausalSignalView[];
    whyOpen: boolean;
    whyError: string | null;
    onToggleWhy: () => void;
    microSteps: MicroStep[];
    onToggleStep: (index: number, checked: boolean) => void;
    closeTabs: TabRecView[];
    overflowCount: number;
    keepCount: number;
    onExpandTabs: () => void;
    hasTabRecommendations: boolean;
    errorAnalysis: { rootCause: string; suggestedFix: string } | null;
    recommended: Array<Record<string, unknown>>;
    executable: Array<Record<string, unknown>>;
    manualCount: number;
    executionMode: ExecutionMode;
    manifestStatus: "pending" | "verified" | "invalid" | null;
    canExecute: boolean;
    apply: ApplyState;
    onApply: () => void;
    onUndo: () => void;
    ratingEligible: boolean;
    rating: "thumbs_up" | "thumbs_down" | null;
    ratingTextOpen: boolean;
    ratingText: string;
    onRate: (rating: "thumbs_up" | "thumbs_down") => void;
    onRatingTextChange: (text: string) => void;
    onRatingTextSubmit: () => void;
    onRatingTextCancel: () => void;
}

export function InterventionCard(props: InterventionCardProps): React.ReactElement {
    const {
        failureBanner, prompt, causalText, causalSignals, whyOpen, whyError, onToggleWhy,
        microSteps, onToggleStep, closeTabs, overflowCount, keepCount, onExpandTabs,
        hasTabRecommendations, errorAnalysis, recommended, executable, manualCount,
        executionMode, manifestStatus, canExecute, apply, onApply, onUndo,
        ratingEligible, rating, ratingTextOpen, ratingText, onRate,
        onRatingTextChange, onRatingTextSubmit, onRatingTextCancel,
    } = props;

    const settled = apply.phase === "applied" || apply.phase === "partial";
    const showRating = ratingEligible && (settled || rating !== null || ratingTextOpen);
    const statusNote = recommended.length === 0
        ? null
        : executionMode === "suggest_only" && executable.length > 0
            ? "Suggestions only — workspace changes are off."
            : manifestStatus === "pending"
                ? "Checking which suggestions can run…"
                : !canExecute
                    ? "Manual review only — no workspace change will run."
                    : null;

    return (
        <div style={S.interventionCard} data-testid="intervention-card">
            {failureBanner && (
                <div
                    data-testid="intervention-error-banner"
                    role="alert"
                    style={{
                        marginBottom: 12,
                        padding: "8px 12px",
                        background: CX.dangerDim,
                        border: `1px solid color-mix(in srgb, ${CX.danger} 36%, transparent)`,
                        borderRadius: CX.radiusSm,
                        color: DANGER_TEXT,
                        fontSize: 12,
                        fontFamily: CX.font,
                        lineHeight: 1.4,
                    }}
                >
                    {failureBanner}
                </div>
            )}

            {prompt && (
                <div
                    data-testid="intervention-prompt"
                    style={{
                        marginBottom: 12,
                        padding: "8px 12px",
                        background: CX.tertiary,
                        borderRadius: CX.radiusSm,
                        color: CX.textSecondary,
                        fontSize: 12,
                        fontFamily: CX.font,
                        lineHeight: 1.4,
                    }}
                >
                    {prompt}
                </div>
            )}

            {causalText && <div style={S.causalText}>{causalText}</div>}

            {(causalSignals.length > 0 || causalText) && (
                <div style={{ marginBottom: 12 }} data-testid="why-drilldown">
                    <button
                        aria-label="Show structured causal rationale"
                        aria-expanded={whyOpen}
                        onClick={onToggleWhy}
                        style={{ ...S.inlineLink, marginTop: 0 }}
                        data-testid="why-toggle"
                    >
                        {whyOpen ? "Hide why" : "Why?"}
                    </button>
                    {whyOpen && whyError && (
                        <div
                            data-testid="why-error"
                            style={{
                                marginTop: 8,
                                padding: "8px 12px",
                                background: CX.tertiary,
                                borderRadius: CX.radiusSm,
                                color: CX.textSecondary,
                                fontSize: 11,
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
                                marginTop: 8,
                                padding: "8px 12px",
                                background: CX.tertiary,
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
                                        <span style={{ fontWeight: isPrimary ? 600 : 500, minWidth: 80 }}>
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
                                                    color: delta < 0 ? CX.danger : CX.accentText,
                                                    fontWeight: 600,
                                                    fontSize: 11,
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

            {microSteps.length > 0 && (
                <div data-testid="micro-step-list" style={{ marginBottom: 12 }}>
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
                                }}
                            >
                                <input
                                    type="checkbox"
                                    data-testid={`micro-step-checkbox-${idx}`}
                                    checked={isDone}
                                    onChange={(e) => onToggleStep(idx, e.target.checked)}
                                    style={{ accentColor: CX.accent, width: 14, height: 14 }}
                                />
                                <span>{step.text}</span>
                            </label>
                        );
                    })}
                </div>
            )}

            {closeTabs.length > 0 && (
                <div style={{ marginBottom: 12 }}>
                    <div style={S.sectionLabel}>Manual tab review</div>
                    {closeTabs.map((tab, i) => (
                        <div key={`c${i}`} style={S.tabRow}>
                            <span style={{ ...S.tabXMark, color: CX.textTertiary }}>{"·"}</span>
                            <span style={S.tabName}>{tab.title}</span>
                        </div>
                    ))}
                    {overflowCount > 0 && (
                        <button style={S.inlineLink} onClick={onExpandTabs}>
                            +{overflowCount} more
                        </button>
                    )}
                    {keepCount > 0 && (
                        <div style={S.keepLine}>Keeping {keepCount} you need</div>
                    )}
                    <div data-testid="manual-tab-review-note" style={S.captionNote}>
                        Cortex won’t close or regroup existing tabs automatically.
                    </div>
                </div>
            )}

            {errorAnalysis && (
                <div style={S.errBox}>
                    <div style={S.errBody}>{errorAnalysis.rootCause}</div>
                    {errorAnalysis.suggestedFix && (
                        <pre style={S.errCode}>{"→ "}{errorAnalysis.suggestedFix}</pre>
                    )}
                </div>
            )}

            {!hasTabRecommendations && !errorAnalysis && recommended.length > 0 && (
                <div style={{ marginBottom: 12 }}>
                    {recommended.map((a, i) => (
                        <div key={i} style={S.tabRow}>
                            <span style={{ ...S.tabXMark, color: CX.textSecondary }}>{"•"}</span>
                            <span style={{ ...S.tabName, color: CX.text }}>{String(a.label || "")}</span>
                        </div>
                    ))}
                    {manualCount > 0 && (
                        <div data-testid="manual-action-review-note" style={S.captionNote}>
                            {executable.length > 0
                                ? "Other items are guidance for manual review."
                                : "Manual review only — no workspace change will run."}
                        </div>
                    )}
                </div>
            )}

            {recommended.length > 0 && canExecute && (
                <ApplyButton
                    apply={apply}
                    label={primaryLabel(executable)}
                    onApply={onApply}
                    onUndo={onUndo}
                />
            )}
            {statusNote && (
                <div
                    data-testid="intervention-status-note"
                    role="status"
                    style={{ ...S.captionNote, textAlign: "center", marginTop: 4 }}
                >
                    {statusNote}
                </div>
            )}

            {showRating && <RatingRow rating={rating} onRate={onRate} />}

            {ratingTextOpen && (
                <input
                    data-testid="rating-text-input"
                    type="text"
                    maxLength={200}
                    placeholder="What would have helped? (Enter to send, Esc to skip)"
                    aria-label="What would have helped?"
                    value={ratingText}
                    onChange={(e) => onRatingTextChange(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === "Enter") onRatingTextSubmit();
                        else if (e.key === "Escape") onRatingTextCancel();
                    }}
                    style={{
                        marginTop: 8,
                        width: "100%",
                        padding: "8px 12px",
                        fontSize: 12,
                        background: CX.tertiary,
                        color: CX.text,
                        border: `1px solid ${CX.accent}`,
                        borderRadius: CX.radiusSm,
                        fontFamily: CX.font,
                        boxSizing: "border-box",
                    }}
                />
            )}
        </div>
    );
}
