/**
 * One quiet-mode control: a segmented radiogroup (Off · Snooze 15m · Quiet ·
 * Pause) with the countdown inline. Replaces the switch, the pill row, and
 * the "turn off" link that used to disagree with each other, and sends only
 * the canonical ``QUIET_MODE_TOGGLE``.
 */

import React from "react";
import { S } from "../styles";

export type QuietModeKind = "off" | "snooze_15" | "quiet_session" | "pause";

const OPTIONS: ReadonlyArray<{ kind: QuietModeKind; label: string }> = [
    { kind: "off", label: "Off" },
    { kind: "snooze_15", label: "Snooze 15m" },
    { kind: "quiet_session", label: "Quiet" },
    { kind: "pause", label: "Pause" },
];

export function quietModeStatus(
    kind: string,
    remainingMin: number | null,
): string {
    const base = kind === "snooze_15"
        ? "Snoozed"
        : kind === "quiet_session"
            ? "Quiet for this session"
            : kind === "pause"
                ? "Paused"
                : "";
    if (!base) return "";
    return remainingMin !== null && remainingMin > 0
        ? `${base} · ${remainingMin}m left`
        : base;
}

export interface QuietModeControlProps {
    kind: string;
    remainingMin: number | null;
    onSelect: (kind: QuietModeKind) => void;
}

export function QuietModeControl({
    kind,
    remainingMin,
    onSelect,
}: QuietModeControlProps): React.ReactElement {
    const status = quietModeStatus(kind, remainingMin);
    const current = OPTIONS.some((option) => option.kind === kind) ? kind : "off";
    return (
        <div>
            <div style={S.toggleRow}>
                <span style={S.toggleLabel} id="cortex-quiet-label">Quiet mode</span>
            </div>
            <div
                style={S.segmented}
                role="radiogroup"
                aria-labelledby="cortex-quiet-label"
                data-testid="quiet-mode-control"
            >
                {OPTIONS.map((option) => {
                    const active = current === option.kind;
                    return (
                        <button
                            key={option.kind}
                            type="button"
                            role="radio"
                            aria-checked={active}
                            data-testid={`quiet-mode-${option.kind}`}
                            onClick={() => { if (!active) onSelect(option.kind); }}
                            style={{
                                ...S.segmentBtn,
                                ...(active ? S.segmentBtnActive : {}),
                            }}
                        >
                            {option.label}
                        </button>
                    );
                })}
            </div>
            <div
                style={S.segmentStatus}
                role="status"
                aria-live="polite"
                data-testid="quiet-mode-status"
            >
                {status}
            </div>
        </div>
    );
}
