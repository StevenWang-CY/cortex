/**
 * Connection surface for the popup.
 *
 * ``ConnectionPanel`` is the single disconnected state: one title, one line
 * of calm guidance, one CTA. ``VersionBanner`` is the non-blocking notice for
 * a major.minor skew between the extension and the app; the live session
 * stays visible beneath it.
 */

import React from "react";
import { CX } from "../../design-tokens";
import type { ConnectivityViewModel } from "../../lib/popup-view-model";
import { S } from "../styles";

export interface ConnectionPanelProps {
    view: ConnectivityViewModel;
    launching: boolean;
    onAction: () => void;
}

export function ConnectionPanel({
    view,
    launching,
    onAction,
}: ConnectionPanelProps): React.ReactElement {
    return (
        <div style={S.disconnectedArea} data-testid="connection-panel">
            <div
                aria-hidden="true"
                style={{
                    width: 40,
                    height: 40,
                    borderRadius: "50%",
                    border: `1.5px solid ${launching ? CX.accent : CX.textTertiary}`,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    transition: `border-color ${CX.durationSlow} ${CX.easeDefault}`,
                    marginBottom: 12,
                }}
            >
                {launching ? (
                    <div
                        className="cortex-motion-ambient"
                        style={{
                            width: 8,
                            height: 8,
                            borderRadius: "50%",
                            background: CX.accent,
                            animation: `cx-pulse 1.5s ${CX.easeInOut} infinite`,
                        }}
                    />
                ) : (
                    <div
                        style={{
                            width: 0,
                            height: 0,
                            borderLeft: `8px solid ${CX.textTertiary}`,
                            borderTop: "5px solid transparent",
                            borderBottom: "5px solid transparent",
                            marginLeft: 2,
                        }}
                    />
                )}
            </div>
            <div style={S.disconnectedTitle} data-testid={view.testId}>
                {view.title}
            </div>
            <div style={S.disconnectedBody} role="status" aria-live="polite">
                {view.body}
            </div>
            <button
                className="cortex-primary-btn"
                data-testid="connection-cta"
                style={{
                    ...S.primaryBtn,
                    marginTop: 16,
                    opacity: view.disabled ? 0.5 : 1,
                    maxWidth: 240,
                }}
                onClick={onAction}
                disabled={view.disabled}
                aria-busy={launching || undefined}
            >
                {view.ctaLabel}
            </button>
        </div>
    );
}

export interface VersionBannerProps {
    view: ConnectivityViewModel;
    onOpen: () => void;
    onDismiss: () => void;
}

export function VersionBanner({
    view,
    onOpen,
    onDismiss,
}: VersionBannerProps): React.ReactElement {
    return (
        <div style={S.bannerRow} role="status" data-testid={view.testId}>
            <div style={{ flex: 1, minWidth: 0 }}>
                <span style={S.bannerTitle}>{view.title}</span>
                <span> · {view.body}</span>
            </div>
            <button style={S.bannerLink} onClick={onOpen} data-testid="version-banner-open">
                {view.ctaLabel}
            </button>
            <button
                aria-label="Dismiss update notice"
                style={{ ...S.iconButton, margin: 0 }}
                onClick={onDismiss}
                data-testid="version-banner-dismiss"
            >{"×"}</button>
        </div>
    );
}
