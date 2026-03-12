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
const DIM_COLOR = "rgba(0, 0, 0, 0.7)";
const ACCENT_COLOR = "#64a0ff";
const BG_COLOR = "rgba(14, 21, 37, 0.95)";
const TEXT_COLOR = "#e6f0ff";
const TEXT_SECONDARY = "#a0b4d2";

// Breathing pacer timing
const INHALE_S = 4;
const HOLD_S = 7;
const EXHALE_S = 8;
const CYCLE_S = INHALE_S + HOLD_S + EXHALE_S;

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
 */
function showOverlay(payload: InterventionPayload): void {
    removeOverlay();

    const host = document.createElement("div");
    host.id = CORTEX_OVERLAY_ID;
    host.style.cssText = `
        position: fixed;
        top: 0; left: 0; right: 0; bottom: 0;
        z-index: 2147483647;
        pointer-events: auto;
    `;

    const shadow = host.attachShadow({ mode: "closed" });

    // Build micro-steps HTML
    let stepsHtml = "";
    for (let i = 0; i < payload.micro_steps.length; i++) {
        const step = escapeHtml(payload.micro_steps[i]);
        stepsHtml += `
            <label class="step">
                <input type="checkbox" id="step-${i}" />
                <span>${step}</span>
            </label>`;
    }

    const dimBg = payload.ui_plan.dim_background;

    shadow.innerHTML = `
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }

            .backdrop {
                position: fixed;
                top: 0; left: 0; right: 0; bottom: 0;
                background: ${dimBg ? DIM_COLOR : "transparent"};
                display: flex;
                align-items: center;
                justify-content: center;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            }

            .card {
                background: ${BG_COLOR};
                border-radius: 16px;
                padding: 28px 32px;
                max-width: 440px;
                width: 90%;
                border-left: 4px solid ${ACCENT_COLOR};
                box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            }

            .headline {
                font-size: 20px;
                font-weight: 700;
                color: ${TEXT_COLOR};
                margin-bottom: 8px;
            }

            .summary {
                font-size: 14px;
                color: ${TEXT_SECONDARY};
                line-height: 1.5;
                margin-bottom: 16px;
            }

            .focus {
                font-size: 14px;
                color: ${ACCENT_COLOR};
                margin-bottom: 16px;
            }

            .steps {
                margin-bottom: 20px;
            }

            .step {
                display: flex;
                align-items: flex-start;
                gap: 10px;
                padding: 8px 0;
                cursor: pointer;
                color: ${TEXT_COLOR};
                font-size: 14px;
            }

            .step input[type="checkbox"] {
                margin-top: 3px;
                accent-color: ${ACCENT_COLOR};
                width: 16px;
                height: 16px;
            }

            .step span {
                line-height: 1.4;
            }

            .pacer {
                text-align: center;
                margin: 20px 0;
            }

            .pacer canvas {
                display: block;
                margin: 0 auto 8px;
            }

            .pacer-label {
                font-size: 15px;
                font-weight: 600;
                color: ${TEXT_COLOR};
            }

            .pacer-timer {
                font-size: 12px;
                color: ${TEXT_SECONDARY};
            }

            .dismiss-btn {
                display: block;
                width: 100%;
                padding: 10px;
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 8px;
                background: rgba(255,255,255,0.06);
                color: ${TEXT_COLOR};
                cursor: pointer;
                font-size: 14px;
                transition: background 0.2s;
            }

            .dismiss-btn:hover {
                background: rgba(255,255,255,0.12);
            }
        </style>

        <div class="backdrop" id="backdrop">
            <div class="card">
                <div class="headline">${escapeHtml(payload.headline)}</div>
                <div class="summary">${escapeHtml(payload.situation_summary)}</div>
                <div class="focus"><strong>Focus:</strong> ${escapeHtml(payload.primary_focus)}</div>
                <div class="steps">${stepsHtml}</div>
                <div class="pacer">
                    <canvas id="pacer-canvas" width="120" height="120"></canvas>
                    <div class="pacer-label" id="pacer-label">Inhale</div>
                    <div class="pacer-timer" id="pacer-timer">4s</div>
                </div>
                <button class="dismiss-btn" id="dismiss-btn">Dismiss</button>
            </div>
        </div>
    `;

    document.body.appendChild(host);

    // --- Event Handlers ---

    const dismissBtn = shadow.getElementById("dismiss-btn");
    if (dismissBtn) {
        dismissBtn.addEventListener("click", () => {
            sendUserAction("dismissed", payload.intervention_id);
            removeOverlay();
        });
    }

    // Escape key dismissal
    const escHandler = (e: KeyboardEvent) => {
        if (e.key === "Escape") {
            sendUserAction("dismissed", payload.intervention_id);
            removeOverlay();
            document.removeEventListener("keydown", escHandler);
        }
    };
    document.addEventListener("keydown", escHandler);

    // Checkbox engagement
    const checkboxes = shadow.querySelectorAll('input[type="checkbox"]');
    checkboxes.forEach((cb) => {
        cb.addEventListener("change", () => {
            sendUserAction("engaged", payload.intervention_id);
        });
    });

    // Backdrop click to dismiss
    const backdrop = shadow.getElementById("backdrop");
    if (backdrop) {
        backdrop.addEventListener("click", (e) => {
            if (e.target === backdrop) {
                sendUserAction("dismissed", payload.intervention_id);
                removeOverlay();
                document.removeEventListener("keydown", escHandler);
            }
        });
    }

    // --- Breathing Pacer Animation ---
    const canvas = shadow.getElementById("pacer-canvas") as HTMLCanvasElement;
    const labelEl = shadow.getElementById("pacer-label");
    const timerEl = shadow.getElementById("pacer-timer");

    if (canvas && labelEl && timerEl) {
        const ctx = canvas.getContext("2d");
        if (ctx) {
            const startTime = performance.now();
            let animFrame: number;

            function drawPacer(): void {
                const elapsed = (performance.now() - startTime) / 1000;
                const cyclePos = elapsed % CYCLE_S;
                const w = canvas.width;
                const h = canvas.height;
                const cx = w / 2;
                const cy = h / 2;
                const maxR = Math.min(w, h) / 2 - 8;

                let phase: string;
                let remaining: number;
                let scale: number;

                if (cyclePos < INHALE_S) {
                    phase = "Inhale";
                    remaining = INHALE_S - cyclePos;
                    scale = 0.3 + 0.7 * (cyclePos / INHALE_S);
                } else if (cyclePos < INHALE_S + HOLD_S) {
                    phase = "Hold";
                    remaining = INHALE_S + HOLD_S - cyclePos;
                    scale = 1.0;
                } else {
                    phase = "Exhale";
                    const exhalePos = cyclePos - INHALE_S - HOLD_S;
                    remaining = EXHALE_S - exhalePos;
                    scale = 1.0 - 0.7 * (exhalePos / EXHALE_S);
                }

                const r = maxR * scale;

                ctx!.clearRect(0, 0, w, h);

                // Layered circles
                for (let i = 0; i < 3; i++) {
                    const ri = r - i * 3;
                    if (ri < 4) break;
                    const alpha = 0.45 - i * 0.12;
                    ctx!.beginPath();
                    ctx!.arc(cx, cy, ri, 0, Math.PI * 2);
                    ctx!.fillStyle = `rgba(100, 160, 255, ${alpha})`;
                    ctx!.fill();
                }

                labelEl!.textContent = phase;
                timerEl!.textContent = `${Math.ceil(remaining)}s`;

                animFrame = requestAnimationFrame(drawPacer);
            }

            animFrame = requestAnimationFrame(drawPacer);

            // Clean up animation when overlay is removed
            const observer = new MutationObserver(() => {
                if (!document.getElementById(CORTEX_OVERLAY_ID)) {
                    cancelAnimationFrame(animFrame);
                    observer.disconnect();
                }
            });
            observer.observe(document.body, { childList: true });
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
