/**
 * Cortex Chrome Extension — Content Script
 *
 * Injected into active tab on intervention triggers.
 * Responsibilities:
 * - DOM text extraction via TreeWalker (≤ 2000 tokens)
 * - Shadow DOM encapsulated UI overlay
 * - Focus overlay (dim rgba(0,0,0,0.7))
 * - Intervention display with headline, micro-steps, dismiss
 * - 4-7-8 breathing pacer animation
 */

// --- Constants ---

const CORTEX_OVERLAY_ID = "cortex-somatic-overlay";
const MAX_TEXT_CHARS = 8000; // ~2000 tokens

// Overlay auto-dismiss timeout (5 minutes)

// --- Overlay Management ---

interface InterventionPayload {
    intervention_id: string;
    level: string;
    headline: string;
    situation_summary: string;
    primary_focus: string;
    micro_steps: string[];
    ui_plan: {
        dim_background: boolean;
        show_overlay: boolean;
        fold_unrelated_code: boolean;
        intervention_type: string;
    };
    tone: string;
}

/**
 * Extract visible page text using TreeWalker.
 * Limited to ~2000 tokens (8000 chars).
 */
function extractPageText(): string {
    const walker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
        {
            acceptNode(node: Text): number {
                const parent = node.parentElement;
                if (!parent) return NodeFilter.FILTER_REJECT;

                const tag = parent.tagName.toLowerCase();
                if (
                    ["script", "style", "noscript", "svg", "path"].includes(tag)
                ) {
                    return NodeFilter.FILTER_REJECT;
                }

                // Skip hidden elements
                const style = getComputedStyle(parent);
                if (
                    style.display === "none" ||
                    style.visibility === "hidden" ||
                    style.opacity === "0"
                ) {
                    return NodeFilter.FILTER_REJECT;
                }

                const text = node.textContent?.trim();
                if (!text || text.length < 2) return NodeFilter.FILTER_REJECT;

                return NodeFilter.FILTER_ACCEPT;
            },
        },
    );

    const chunks: string[] = [];
    let totalLen = 0;
    let current: Text | null;

    while ((current = walker.nextNode() as Text | null)) {
        const text = current.textContent?.trim();
        if (!text) continue;

        if (totalLen + text.length > MAX_TEXT_CHARS) {
            chunks.push(text.substring(0, MAX_TEXT_CHARS - totalLen));
            break;
        }
        chunks.push(text);
        totalLen += text.length;
    }

    return chunks.join(" ");
}

/**
 * Remove any existing Cortex overlay.
 */
function removeOverlay(): void {
    const existing = document.getElementById(CORTEX_OVERLAY_ID);
    if (existing) {
        existing.remove();
    }
}

/**
 * Show the intervention overlay with Shadow DOM encapsulation.
 *
 * Design: a gentle bottom-right panel that slides in with a soft backdrop.
 * Breathing pacer uses smooth CSS animations. Minimal, warm, and calming.
 */
function showOverlay(payload: InterventionPayload): void {
    removeOverlay();

    const host = document.createElement("div");
    host.id = CORTEX_OVERLAY_ID;
    host.style.cssText = `
        position: fixed;
        top: 0; left: 0; right: 0; bottom: 0;
        z-index: 2147483647;
        pointer-events: none;
    `;

    const shadow = host.attachShadow({ mode: "closed" });

    // Build micro-steps HTML
    let stepsHtml = "";
    for (let i = 0; i < payload.micro_steps.length; i++) {
        const step = escapeHtml(payload.micro_steps[i]);
        stepsHtml += `
            <div class="step" id="step-row-${i}">
                <div class="step-dot" id="step-dot-${i}"></div>
                <span>${step}</span>
            </div>`;
    }

    const dimBg = payload.ui_plan.dim_background;

    shadow.innerHTML = `
        <style>
            @keyframes panelIn {
                from { transform: translateY(12px) scale(.99); opacity: 0; }
                to   { transform: translateY(0) scale(1); opacity: 1; }
            }
            @keyframes fadeIn {
                from { opacity: 0; }
                to   { opacity: 1; }
            }

            * { box-sizing: border-box; margin: 0; padding: 0; }

            .scrim {
                position: fixed;
                top: 0; left: 0; right: 0; bottom: 0;
                background: ${dimBg ? "rgba(0, 0, 0, 0.35)" : "transparent"};
                pointer-events: ${dimBg ? "auto" : "none"};
                animation: fadeIn 0.25s ease;
            }

            .panel {
                position: fixed;
                bottom: 20px;
                right: 20px;
                width: 340px;
                max-height: calc(100vh - 40px);
                overflow-y: auto;
                pointer-events: auto;

                background: #111113;
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.06);
                box-shadow:
                    0 0 0 .5px rgba(0,0,0,.3),
                    0 4px 20px rgba(0,0,0,.4),
                    0 16px 40px rgba(0,0,0,.2);

                font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'SF Pro Text', system-ui, sans-serif;
                animation: panelIn 0.3s cubic-bezier(0.16, 1, 0.3, 1);
                color: #e4e4e7;
            }
            .panel::-webkit-scrollbar { width: 0; }

            .panel-inner {
                padding: 18px 16px 14px;
            }

            .header {
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
                gap: 12px;
                margin-bottom: 4px;
            }

            .headline {
                font-size: 13px;
                font-weight: 600;
                letter-spacing: -0.2px;
                line-height: 1.4;
                color: #e4e4e7;
            }

            .close-btn {
                flex-shrink: 0;
                width: 22px;
                height: 22px;
                border: none;
                background: rgba(255, 255, 255, 0.04);
                border-radius: 6px;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: background 0.12s;
                margin-top: 1px;
            }
            .close-btn:hover { background: rgba(255, 255, 255, 0.08); }
            .close-btn svg {
                width: 9px; height: 9px;
                stroke: #71717a; stroke-width: 2;
            }

            .summary {
                font-size: 12px;
                color: #71717a;
                line-height: 1.5;
                margin-bottom: 14px;
            }

            .divider {
                height: 1px;
                background: rgba(255, 255, 255, 0.04);
                margin: 0 0 12px;
            }

            .section-label {
                font-size: 11px;
                font-weight: 500;
                color: #71717a;
                margin-bottom: 6px;
            }

            .steps {
                margin-bottom: 14px;
            }

            .step {
                display: flex;
                align-items: flex-start;
                gap: 10px;
                padding: 4px 0;
                cursor: pointer;
                transition: opacity 0.2s;
            }
            .step.done span {
                color: #3f3f46;
                text-decoration: line-through;
                text-decoration-color: rgba(255,255,255,.06);
            }

            .step-dot {
                flex-shrink: 0;
                width: 16px;
                height: 16px;
                border-radius: 50%;
                border: 1.5px solid #3f3f46;
                margin-top: 1px;
                transition: all 0.2s ease;
                position: relative;
            }
            .step.done .step-dot {
                background: #10b981;
                border-color: #10b981;
            }
            .step.done .step-dot::after {
                content: '';
                position: absolute;
                top: 3px; left: 4.5px;
                width: 4px; height: 7px;
                border: solid white;
                border-width: 0 1.5px 1.5px 0;
                transform: rotate(45deg);
            }

            .step span {
                font-size: 12px;
                line-height: 1.5;
                color: #e4e4e7;
            }

            /* Dismiss */
            .dismiss-btn {
                display: block;
                width: 100%;
                padding: 6px;
                border: none;
                border-radius: 6px;
                background: none;
                color: #3f3f46;
                cursor: pointer;
                font-size: 11px;
                font-family: inherit;
                transition: color 0.12s;
            }
            .dismiss-btn:hover { color: #71717a; }
        </style>

        <div class="scrim" id="scrim"></div>
        <div class="panel" id="panel">
            <div class="panel-inner">
                <div class="header">
                    <div class="headline">${escapeHtml(payload.headline)}</div>
                    <button class="close-btn" id="close-btn" aria-label="Close">
                        <svg viewBox="0 0 10 10" fill="none"><path d="M1 1l8 8M9 1l-8 8"/></svg>
                    </button>
                </div>
                <div class="summary">${escapeHtml(payload.situation_summary)}</div>
                <div class="divider"></div>
                <div class="section-label">Next steps</div>
                <div class="steps">${stepsHtml}</div>
                <button class="dismiss-btn" id="dismiss-btn">Dismiss</button>
            </div>
        </div>
    `;

    document.body.appendChild(host);

    // --- Event Handlers ---

    const dismiss = () => {
        sendUserAction("dismissed", payload.intervention_id);
        removeOverlay();
        document.removeEventListener("keydown", escHandler);
    };

    const closeBtn = shadow.getElementById("close-btn");
    if (closeBtn) closeBtn.addEventListener("click", dismiss);

    const dismissBtn = shadow.getElementById("dismiss-btn");
    if (dismissBtn) dismissBtn.addEventListener("click", dismiss);

    const escHandler = (e: KeyboardEvent) => {
        if (e.key === "Escape") dismiss();
    };
    document.addEventListener("keydown", escHandler);

    // Scrim click to dismiss (if dimmed)
    const scrim = shadow.getElementById("scrim");
    if (scrim) scrim.addEventListener("click", dismiss);

    // Click-to-complete steps
    for (let i = 0; i < payload.micro_steps.length; i++) {
        const row = shadow.getElementById(`step-row-${i}`);
        if (row) {
            row.addEventListener("click", () => {
                row.classList.toggle("done");
                sendUserAction("engaged", payload.intervention_id);
            });
        }
    }

    // Auto-dismiss after 5 minutes
    setTimeout(() => {
        if (document.getElementById(CORTEX_OVERLAY_ID)) {
            sendUserAction("dismissed", payload.intervention_id);
            removeOverlay();
        }
    }, 5 * 60 * 1000);
}

// --- Helpers ---

function escapeHtml(text: string): string {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function sendUserAction(action: string, interventionId: string): void {
    try {
        chrome.runtime.sendMessage({
            type: "USER_ACTION",
            action,
            intervention_id: interventionId,
        });
    } catch {
        // Extension context may be invalidated
    }
}

// --- Message Listener ---

chrome.runtime.onMessage.addListener(
    (
        message: Record<string, unknown>,
        _sender: chrome.runtime.MessageSender,
        sendResponse: (response: unknown) => void,
    ) => {
        switch (message.type) {
            case "SHOW_INTERVENTION":
                showOverlay(message.payload as InterventionPayload);
                sendResponse({ ok: true });
                break;

            case "REMOVE_OVERLAY":
                removeOverlay();
                sendResponse({ ok: true });
                break;

            case "EXTRACT_TEXT":
                sendResponse({ text: extractPageText() });
                break;
        }
        return false;
    },
);
