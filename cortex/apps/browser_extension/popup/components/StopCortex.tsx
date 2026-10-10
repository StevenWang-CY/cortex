/**
 * Stop Cortex — a destructive action that names its consequence and asks
 * once. Lives below its own divider, visually quiet until the user reaches
 * for it; the confirm step carries the only danger fill in the popup.
 */

import React, { useEffect, useRef, useState } from "react";
import { S } from "../styles";

export interface StopCortexProps {
    stopping: boolean;
    /** Sticky intent controls reconnect; it is not proof of daemon exit. */
    stopRequested: boolean;
    confirmed?: boolean;
    error?: string | null;
    onStop: () => void;
}

export function StopCortex({
    stopping,
    stopRequested,
    confirmed = false,
    error = null,
    onStop,
}: StopCortexProps): React.ReactElement {
    const [confirming, setConfirming] = useState(false);
    const stopButtonRef = useRef<HTMLButtonElement>(null);
    const triggerRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        if (confirming) stopButtonRef.current?.focus({ preventScroll: true });
    }, [confirming]);

    useEffect(() => {
        // Confirmation belongs to one stop attempt. Starting a new session
        // must return to the initial control, not revive the previous prompt.
        if (stopping || confirmed) setConfirming(false);
    }, [stopping, confirmed]);

    const cancel = () => {
        setConfirming(false);
        window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
    };

    if (error && !stopping) {
        return (
            <div style={S.stopConfirm}>
                <div style={S.stopConfirmText} role="alert">{error}</div>
                <button type="button" style={S.stopBtn} onClick={onStop} data-testid="stop-cortex-retry">
                    Retry stop
                </button>
            </div>
        );
    }

    if (confirmed && !stopping) {
        return (
            <div style={S.stoppedNote} role="status" data-testid="stop-cortex-stopped">
                Cortex is stopped. Use Open Cortex above to start it again.
            </div>
        );
    }

    if (confirming || stopping) {
        return (
            <div
                style={S.stopConfirm}
                role="group"
                aria-labelledby="cortex-stop-confirm-text"
                data-testid="stop-cortex-confirm"
                onKeyDown={(event) => {
                    if (event.key === "Escape" && !stopping) {
                        event.preventDefault();
                        cancel();
                    }
                }}
            >
                <div style={S.stopConfirmText} id="cortex-stop-confirm-text">
                    {stopping
                        ? "Stopping Cortex and finishing your session…"
                        : "Stop Cortex? The camera and the Cortex app will shut down."}
                </div>
                <div style={S.stopConfirmRow}>
                    <button
                        type="button"
                        style={S.cancelBtn}
                        onClick={cancel}
                        disabled={stopping}
                        data-testid="stop-cortex-cancel"
                    >
                        Cancel
                    </button>
                    <button
                        ref={stopButtonRef}
                        type="button"
                        style={{ ...S.dangerBtn, opacity: stopping ? 0.6 : 1 }}
                        onClick={() => { if (!stopping) onStop(); }}
                        disabled={stopping}
                        aria-busy={stopping || undefined}
                        data-testid="stop-cortex-confirm-button"
                    >
                        {stopping ? "Stopping…" : "Stop"}
                    </button>
                </div>
            </div>
        );
    }

    return (
        <div>
        {stopRequested && <div style={S.stoppedNote} role="status">
            Automatic reconnect is paused. Shutdown has not been confirmed.
        </div>}
        <button
            ref={triggerRef}
            type="button"
            style={S.stopBtn}
            onClick={() => setConfirming(true)}
            data-testid="stop-cortex"
        >
            Stop Cortex
        </button>
        </div>
    );
}
