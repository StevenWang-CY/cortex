/**
 * Cortex Chrome Extension — Content Script
 *
 * Injected into every page. Responsibilities:
 * - DOM text extraction via TreeWalker (≤ 2000 tokens)
 * - Shadow DOM intervention overlay (fallback for executeScript failures)
 * - Ambient somatic feedback: aura vignette, color temperature filter,
 *   weather particles, and flow shield — all sub-threshold
 */

// --- Constants ---

const CORTEX_OVERLAY_ID = "cortex-somatic-overlay";
const CORTEX_AMBIENT_ID = "cortex-ambient-layer";
const MAX_TEXT_CHARS = 8000; // ~2000 tokens

// State colors (matches design system)
const STATE_COLORS: Record<string, { r: number; g: number; b: number }> = {
    FLOW: { r: 16, g: 185, b: 129 },     // emerald
    HYPER: { r: 239, g: 68, b: 68 },      // red
    HYPO: { r: 59, g: 130, b: 246 },      // blue
    RECOVERY: { r: 245, g: 158, b: 11 },  // amber
};

// Somatic filter: warm = stressed, cool = focused
const SOMATIC_TEMPS: Record<string, { r: number; g: number; b: number; opacity: number }> = {
    FLOW: { r: 100, g: 180, b: 220, opacity: 0.015 },     // cool blue tint
    HYPER: { r: 230, g: 160, b: 100, opacity: 0.035 },     // warm amber tint
    HYPO: { r: 140, g: 160, b: 210, opacity: 0.02 },       // muted cool
    RECOVERY: { r: 200, g: 180, b: 140, opacity: 0.02 },   // neutral warm
};

// Flow shield: known distraction element selectors per domain
const FLOW_SHIELD_SELECTORS: Record<string, string[]> = {
    "youtube.com": ["#secondary", "#related", "#comments", "ytd-rich-section-renderer"],
    "twitter.com": ["[data-testid='trend']", "aside[role='complementary']", "[data-testid='sidebarColumn']"],
    "x.com": ["[data-testid='trend']", "aside[role='complementary']", "[data-testid='sidebarColumn']"],
    "reddit.com": [".sidebar", ".side", "[data-testid='subreddit-sidebar']"],
    "github.com": [".feed-item", ".js-notice", "#dashboard .news"],
};

// --- Ambient State ---

interface AmbientState {
    state: string;
    confidence: number;
    inFocus: boolean;  // whether the user is in FLOW state
}

let ambientState: AmbientState = { state: "", confidence: 0, inFocus: false };
let ambientContainer: HTMLElement | null = null;
let auraEl: HTMLElement | null = null;
let somaticEl: HTMLElement | null = null;
let particleCanvas: HTMLCanvasElement | null = null;
let particleCtx: CanvasRenderingContext2D | null = null;
let particleRaf: number | null = null;
let flowShieldActive = false;
let flowShieldStartTime = 0;
const FLOW_SHIELD_RAMP_MS = 180_000; // 3 minutes to full effect

interface Particle {
    x: number;
    y: number;
    vx: number;
    vy: number;
    size: number;
    opacity: number;
    life: number;
}

let particles: Particle[] = [];

// --- Text Extraction ---

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
    causal_explanation?: string;
    suggested_actions?: Array<{
        action_id: string;
        action_type: string;
        tab_index?: number;
        target?: string;
        label: string;
        reason?: string;
        category?: string;
        reversible?: boolean;
    }>;
    tab_recommendations?: {
        tabs: Array<{ tab_index: number; tab_title: string; action: string; reason: string }>;
        summary: string;
    };
}

function removeOverlay(): void {
    const existing = document.getElementById(CORTEX_OVERLAY_ID);
    if (existing) existing.remove();
}

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

    const causalHtml = payload.causal_explanation ? `
        <div class="causal-section" id="causal-toggle">
            <div class="causal-header">
                <span class="causal-label">Why this?</span>
                <span class="causal-chevron" id="causal-chevron">›</span>
            </div>
            <div class="causal-body" id="causal-body" style="display:none;">
                ${escapeHtml(payload.causal_explanation)}
            </div>
        </div>
    ` : "";

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
                position: fixed; inset: 0;
                background: ${dimBg ? "rgba(0, 0, 0, 0.35)" : "transparent"};
                pointer-events: ${dimBg ? "auto" : "none"};
                animation: fadeIn 0.25s ease;
            }
            .panel {
                position: fixed; bottom: 20px; right: 20px; width: 340px;
                max-height: calc(100vh - 40px); overflow-y: auto; pointer-events: auto;
                background: #111113; border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.06);
                box-shadow: 0 0 0 .5px rgba(0,0,0,.3), 0 4px 20px rgba(0,0,0,.4), 0 16px 40px rgba(0,0,0,.2);
                font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'SF Pro Text', system-ui, sans-serif;
                animation: panelIn 0.3s cubic-bezier(0.16, 1, 0.3, 1); color: #e4e4e7;
            }
            .panel::-webkit-scrollbar { width: 0; }
            .panel-inner { padding: 18px 16px 14px; }
            .header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
            .headline { font-size: 13px; font-weight: 600; letter-spacing: -0.2px; line-height: 1.4; color: #e4e4e7; }
            .close-btn { flex-shrink: 0; width: 22px; height: 22px; border: none; background: rgba(255,255,255,.04); border-radius: 6px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background .12s; margin-top: 1px; }
            .close-btn:hover { background: rgba(255,255,255,.08); }
            .close-btn svg { width: 9px; height: 9px; stroke: #71717a; stroke-width: 2; }
            .summary { font-size: 12px; color: #71717a; line-height: 1.5; margin-bottom: 14px; }
            .divider { height: 1px; background: rgba(255,255,255,.04); margin: 0 0 12px; }
            .section-label { font-size: 11px; font-weight: 500; color: #71717a; margin-bottom: 6px; }
            .steps { margin-bottom: 14px; }
            .step { display: flex; align-items: flex-start; gap: 10px; padding: 4px 0; cursor: pointer; transition: opacity .2s; }
            .step.done span { color: #3f3f46; text-decoration: line-through; text-decoration-color: rgba(255,255,255,.06); }
            .step-dot { flex-shrink: 0; width: 16px; height: 16px; border-radius: 50%; border: 1.5px solid #3f3f46; margin-top: 1px; transition: all .2s ease; position: relative; }
            .step.done .step-dot { background: #10b981; border-color: #10b981; }
            .step.done .step-dot::after { content: ''; position: absolute; top: 3px; left: 4.5px; width: 4px; height: 7px; border: solid white; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); }
            .step span { font-size: 12px; line-height: 1.5; color: #e4e4e7; }
            .dismiss-btn { display: block; width: 100%; padding: 6px; border: none; border-radius: 6px; background: none; color: #3f3f46; cursor: pointer; font-size: 11px; font-family: inherit; transition: color .12s; }
            .dismiss-btn:hover { color: #71717a; }
            .causal-section { margin-bottom: 10px; }
            .causal-header { display: flex; align-items: center; gap: 4px; cursor: pointer; }
            .causal-label { font-size: 11px; color: #3f3f46; font-weight: 500; }
            .causal-chevron { font-size: 11px; color: #3f3f46; transition: transform .15s; }
            .causal-chevron.open { transform: rotate(90deg); }
            .causal-body { font-size: 11px; color: #52525b; line-height: 1.5; padding: 6px 0 2px; }
            .rating-row { display: flex; justify-content: center; gap: 8px; margin-top: 6px; }
            .rating-btn { width: 28px; height: 28px; border: 1px solid rgba(255,255,255,.06); border-radius: 6px; background: none; cursor: pointer; font-size: 12px; display: flex; align-items: center; justify-content: center; transition: background .12s; }
            .rating-btn:hover { background: rgba(255,255,255,.06); }
            .rating-btn.selected { background: rgba(16,185,129,.15); border-color: rgba(16,185,129,.2); }
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
                ${causalHtml}
                <div class="divider"></div>
                <div class="section-label">Next steps</div>
                <div class="steps">${stepsHtml}</div>
                <button class="dismiss-btn" id="dismiss-btn">Dismiss</button>
                <div class="rating-row">
                    <button class="rating-btn" id="thumbs-up" aria-label="Helpful">👍</button>
                    <button class="rating-btn" id="thumbs-down" aria-label="Not helpful">👎</button>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(host);

    const dismiss = () => {
        sendUserAction("dismissed", payload.intervention_id);
        removeOverlay();
        document.removeEventListener("keydown", escHandler);
    };

    shadow.getElementById("close-btn")?.addEventListener("click", dismiss);
    shadow.getElementById("dismiss-btn")?.addEventListener("click", dismiss);
    shadow.getElementById("scrim")?.addEventListener("click", dismiss);

    const escHandler = (e: KeyboardEvent) => {
        if (e.key === "Escape") dismiss();
    };
    document.addEventListener("keydown", escHandler);

    const causalToggle = shadow.getElementById("causal-toggle");
    const causalBody = shadow.getElementById("causal-body");
    const causalChevron = shadow.getElementById("causal-chevron");
    if (causalToggle && causalBody && causalChevron) {
        causalToggle.addEventListener("click", () => {
            const isOpen = causalBody.style.display !== "none";
            causalBody.style.display = isOpen ? "none" : "block";
            causalChevron.classList.toggle("open", !isOpen);
        });
    }

    const thumbsUp = shadow.getElementById("thumbs-up");
    const thumbsDown = shadow.getElementById("thumbs-down");
    if (thumbsUp) {
        thumbsUp.addEventListener("click", () => {
            thumbsUp.classList.add("selected");
            thumbsDown?.classList.remove("selected");
            chrome.runtime.sendMessage({
                type: "USER_RATING",
                intervention_id: payload.intervention_id,
                rating: "thumbs_up",
            });
        });
    }
    if (thumbsDown) {
        thumbsDown.addEventListener("click", () => {
            thumbsDown.classList.add("selected");
            thumbsUp?.classList.remove("selected");
            chrome.runtime.sendMessage({
                type: "USER_RATING",
                intervention_id: payload.intervention_id,
                rating: "thumbs_down",
            });
        });
    }

    for (let i = 0; i < payload.micro_steps.length; i++) {
        const row = shadow.getElementById(`step-row-${i}`);
        if (row) {
            row.addEventListener("click", () => {
                row.classList.toggle("done");
                sendUserAction("engaged", payload.intervention_id);
            });
        }
    }

    setTimeout(() => {
        if (document.getElementById(CORTEX_OVERLAY_ID)) {
            sendUserAction("dismissed", payload.intervention_id);
            removeOverlay();
        }
    }, 5 * 60 * 1000);
}

// --- Ambient Somatic Feedback ---

/**
 * Initialize the ambient feedback container. Creates three layers:
 * 1. Aura — radial gradient vignette (color shifts with state)
 * 2. Somatic filter — full-screen color temperature overlay (mix-blend-mode)
 * 3. Weather particles — canvas with state-dependent particle animation
 *
 * All layers are sub-threshold: barely perceptible, never distracting.
 */
function initAmbient(): void {
    if (ambientContainer) return;

    ambientContainer = document.createElement("div");
    ambientContainer.id = CORTEX_AMBIENT_ID;
    ambientContainer.style.cssText =
        "position:fixed;inset:0;z-index:2147483640;pointer-events:none;";

    // Layer 1: Aura vignette
    auraEl = document.createElement("div");
    auraEl.style.cssText =
        "position:absolute;inset:0;opacity:0;transition:opacity 3s ease,background 3s ease;pointer-events:none;";
    ambientContainer.appendChild(auraEl);

    // Layer 2: Somatic color temperature filter
    somaticEl = document.createElement("div");
    somaticEl.style.cssText =
        "position:absolute;inset:0;opacity:0;mix-blend-mode:multiply;transition:opacity 45s ease,background 45s ease;pointer-events:none;";
    ambientContainer.appendChild(somaticEl);

    // Layer 3: Weather particles canvas
    particleCanvas = document.createElement("canvas");
    particleCanvas.style.cssText =
        "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;";
    ambientContainer.appendChild(particleCanvas);
    particleCtx = particleCanvas.getContext("2d");

    document.documentElement.appendChild(ambientContainer);

    // Size canvas
    resizeParticleCanvas();
    window.addEventListener("resize", resizeParticleCanvas);

    // Start particle loop
    startParticleLoop();
}

function resizeParticleCanvas(): void {
    if (!particleCanvas) return;
    const dpr = window.devicePixelRatio || 1;
    particleCanvas.width = window.innerWidth * dpr;
    particleCanvas.height = window.innerHeight * dpr;
    if (particleCtx) particleCtx.scale(dpr, dpr);
}

/**
 * Update all ambient layers based on new state.
 */
function updateAmbient(payload: Record<string, unknown>): void {
    const state = (payload.state as string) || "";
    const confidence = (payload.confidence as number) || 0;

    ambientState = {
        state,
        confidence,
        inFocus: state === "FLOW" || state === "RECOVERY",
    };

    if (!ambientContainer) initAmbient();

    updateAura(state, confidence);
    updateSomaticFilter(state);
    updateFlowShield(state);
    updateParticleTarget(state);
}

function updateAura(state: string, confidence: number): void {
    if (!auraEl) return;
    const col = STATE_COLORS[state] || STATE_COLORS.FLOW;
    // Vignette: radial gradient from transparent center to subtle colored edges
    const alpha = Math.min(confidence * 0.03, 0.03); // max 3% opacity at edges
    auraEl.style.background =
        `radial-gradient(ellipse at center, transparent 50%, rgba(${col.r},${col.g},${col.b},${alpha}) 100%)`;
    auraEl.style.opacity = state ? "1" : "0";
}

function updateSomaticFilter(state: string): void {
    if (!somaticEl) return;
    const temp = SOMATIC_TEMPS[state] || SOMATIC_TEMPS.FLOW;
    somaticEl.style.background = `rgba(${temp.r},${temp.g},${temp.b},1)`;
    somaticEl.style.opacity = state ? String(temp.opacity) : "0";
}

// --- Weather Particles ---

let targetParticleCount = 0;
let lastFrameTime = 0;
const PARTICLE_FPS = 15;
const FRAME_INTERVAL = 1000 / PARTICLE_FPS;

function updateParticleTarget(state: string): void {
    switch (state) {
        case "HYPER":
            targetParticleCount = 35; // rain-like, stressed
            break;
        case "HYPO":
            targetParticleCount = 12; // slow drift
            break;
        case "FLOW":
            targetParticleCount = 6; // minimal, calm
            break;
        case "RECOVERY":
            targetParticleCount = 10;
            break;
        default:
            targetParticleCount = 0;
    }
}

function spawnParticle(): Particle {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const isStressed = ambientState.state === "HYPER";

    return {
        x: Math.random() * w,
        y: isStressed ? -10 : Math.random() * h, // rain from top when stressed
        vx: (Math.random() - 0.5) * (isStressed ? 0.3 : 0.15),
        vy: isStressed ? 1.5 + Math.random() * 2 : (Math.random() - 0.5) * 0.2,
        size: isStressed ? 1 : 1.5 + Math.random(),
        opacity: 0.03 + Math.random() * 0.04, // 3-7% opacity
        life: 1,
    };
}

function startParticleLoop(): void {
    if (particleRaf !== null) return;

    function frame(now: number) {
        particleRaf = requestAnimationFrame(frame);

        // Throttle to ~15fps
        if (now - lastFrameTime < FRAME_INTERVAL) return;
        lastFrameTime = now;

        if (!particleCtx || !particleCanvas) return;
        const w = window.innerWidth;
        const h = window.innerHeight;

        particleCtx.clearRect(0, 0, w, h);

        // Add/remove particles toward target
        while (particles.length < targetParticleCount) {
            particles.push(spawnParticle());
        }
        if (particles.length > targetParticleCount) {
            // Fade out extras
            for (let i = targetParticleCount; i < particles.length; i++) {
                particles[i].life -= 0.02;
            }
            particles = particles.filter(p => p.life > 0);
        }

        const col = STATE_COLORS[ambientState.state] || STATE_COLORS.FLOW;

        for (const p of particles) {
            p.x += p.vx;
            p.y += p.vy;

            // Wrap around
            if (p.y > h + 10) { p.y = -10; p.x = Math.random() * w; }
            if (p.y < -20) { p.y = h + 10; }
            if (p.x > w + 10) p.x = -10;
            if (p.x < -10) p.x = w + 10;

            const alpha = p.opacity * p.life;
            particleCtx.beginPath();

            if (ambientState.state === "HYPER") {
                // Rain: vertical streaks
                particleCtx.moveTo(p.x, p.y);
                particleCtx.lineTo(p.x + p.vx * 2, p.y + p.vy * 3);
                particleCtx.strokeStyle = `rgba(${col.r},${col.g},${col.b},${alpha})`;
                particleCtx.lineWidth = 0.8;
                particleCtx.stroke();
            } else {
                // Calm: gentle dots
                particleCtx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                particleCtx.fillStyle = `rgba(${col.r},${col.g},${col.b},${alpha})`;
                particleCtx.fill();
            }
        }
    }

    particleRaf = requestAnimationFrame(frame);
}

// --- Flow Shield ---

const shieldedElements = new Set<HTMLElement>();

function updateFlowShield(state: string): void {
    const isFocused = state === "FLOW";

    if (isFocused && !flowShieldActive) {
        flowShieldActive = true;
        flowShieldStartTime = Date.now();
    } else if (!isFocused && flowShieldActive) {
        flowShieldActive = false;
        restoreShieldedElements();
        return;
    }

    if (!flowShieldActive) return;

    // Calculate fade progress (0→1 over 3 minutes)
    const elapsed = Date.now() - flowShieldStartTime;
    const progress = Math.min(elapsed / FLOW_SHIELD_RAMP_MS, 1);
    // Target opacity: fade to 5% over 3 minutes
    const targetOpacity = 1 - (progress * 0.95); // 1.0 → 0.05

    const hostname = window.location.hostname.replace("www.", "");
    const selectors = Object.entries(FLOW_SHIELD_SELECTORS)
        .filter(([domain]) => hostname.includes(domain))
        .flatMap(([, sels]) => sels);

    if (selectors.length === 0) return;

    for (const sel of selectors) {
        try {
            const els = document.querySelectorAll<HTMLElement>(sel);
            for (const el of els) {
                if (!shieldedElements.has(el)) {
                    // Save original opacity
                    el.dataset.cortexOriginalOpacity = el.style.opacity || "";
                    el.style.transition = "opacity 30s ease";
                    shieldedElements.add(el);
                }
                el.style.opacity = String(targetOpacity);
            }
        } catch {
            // Invalid selector
        }
    }
}

function restoreShieldedElements(): void {
    for (const el of shieldedElements) {
        el.style.opacity = el.dataset.cortexOriginalOpacity || "";
        el.style.transition = "";
        delete el.dataset.cortexOriginalOpacity;
    }
    shieldedElements.clear();
}

// Update flow shield periodically (since it's time-based)
setInterval(() => {
    if (flowShieldActive) {
        updateFlowShield(ambientState.state);
    }
}, 10_000); // every 10s

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

// --- Breathing Overlay ---

function showBreathingOverlay(payload: Record<string, unknown>): void {
    removeOverlay();

    const host = document.createElement("div");
    host.id = CORTEX_OVERLAY_ID;
    host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";

    const shadow = host.attachShadow({ mode: "closed" });

    const headline = escapeHtml(String(payload.headline || "Take a breath"));

    shadow.innerHTML = `
        <style>
            @keyframes breathe {
                0%, 100% { transform: scale(0.6); opacity: 0.3; }
                21% { transform: scale(1); opacity: 0.6; }  /* 4s inhale at ~19s cycle */
                58% { transform: scale(1); opacity: 0.6; }  /* 7s hold */
                100% { transform: scale(0.6); opacity: 0.3; } /* 8s exhale */
            }
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            * { box-sizing: border-box; margin: 0; padding: 0; }
            .scrim {
                position: fixed; inset: 0;
                background: rgba(0, 0, 0, 0.5);
                pointer-events: auto;
                animation: fadeIn 1s ease;
                display: flex; align-items: center; justify-content: center; flex-direction: column;
            }
            .circle {
                width: 120px; height: 120px; border-radius: 50%;
                background: radial-gradient(circle, rgba(16,185,129,.4), rgba(16,185,129,.05));
                animation: breathe 19s ease-in-out infinite;
            }
            .label { color: #a1a1aa; font-size: 13px; margin-top: 20px; font-family: -apple-system, system-ui, sans-serif; }
            .phase { color: #e4e4e7; font-size: 16px; margin-top: 8px; font-weight: 300; font-family: -apple-system, system-ui, sans-serif; }
            .headline { color: #e4e4e7; font-size: 12px; margin-top: 24px; max-width: 280px; text-align: center; line-height: 1.5; font-family: -apple-system, system-ui, sans-serif; }
            .dismiss { color: #3f3f46; font-size: 11px; margin-top: 16px; cursor: pointer; background: none; border: none; font-family: -apple-system, system-ui, sans-serif; }
            .dismiss:hover { color: #71717a; }
        </style>
        <div class="scrim" id="scrim">
            <div class="circle" id="circle"></div>
            <div class="label">4-7-8 breathing</div>
            <div class="phase" id="phase">Inhale...</div>
            <div class="headline">${headline}</div>
            <button class="dismiss" id="dismiss-btn">Skip</button>
        </div>
    `;

    document.body.appendChild(host);

    // Animate phase text
    const phaseEl = shadow.getElementById("phase");
    if (phaseEl) {
        const phases = [
            { text: "Inhale...", duration: 4000 },
            { text: "Hold...", duration: 7000 },
            { text: "Exhale...", duration: 8000 },
        ];
        let idx = 0;
        const cyclePhase = () => {
            if (!document.getElementById(CORTEX_OVERLAY_ID)) return;
            phaseEl.textContent = phases[idx].text;
            setTimeout(() => { idx = (idx + 1) % phases.length; cyclePhase(); }, phases[idx].duration);
        };
        cyclePhase();
    }

    const dismiss = () => {
        sendUserAction("dismissed", String(payload.intervention_id || ""));
        removeOverlay();
    };
    shadow.getElementById("dismiss-btn")?.addEventListener("click", dismiss);
    shadow.getElementById("scrim")?.addEventListener("click", (e) => {
        if (e.target === shadow.getElementById("scrim")) dismiss();
    });

    // Auto-dismiss after 3 cycles (57 seconds)
    setTimeout(() => {
        if (document.getElementById(CORTEX_OVERLAY_ID)) {
            removeOverlay();
        }
    }, 57_000);
}

// --- Active Recall ---

function showActiveRecall(payload: Record<string, unknown>): void {
    removeOverlay();

    const host = document.createElement("div");
    host.id = CORTEX_OVERLAY_ID;
    host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";

    const shadow = host.attachShadow({ mode: "closed" });

    const question = escapeHtml(String(payload.recall_question || "What was the key concept?"));
    const answer = String(payload.recall_answer || "");
    const headline = escapeHtml(String(payload.headline || "Quick check"));

    shadow.innerHTML = `
        <style>
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            * { box-sizing: border-box; margin: 0; padding: 0; }
            .scrim {
                position: fixed; inset: 0;
                backdrop-filter: blur(12px);
                background: rgba(0,0,0,0.6);
                pointer-events: auto;
                animation: fadeIn 0.5s ease;
                display: flex; align-items: center; justify-content: center;
            }
            .card {
                background: #111113; border-radius: 12px; padding: 24px;
                border: 1px solid rgba(255,255,255,.06); max-width: 380px; width: 90%;
                box-shadow: 0 16px 40px rgba(0,0,0,.4);
                font-family: -apple-system, system-ui, sans-serif;
            }
            .title { font-size: 11px; color: #71717a; margin-bottom: 12px; letter-spacing: 0.5px; font-weight: 500; }
            .question { font-size: 14px; color: #e4e4e7; line-height: 1.6; margin-bottom: 16px; }
            .input-row { display: flex; gap: 8px; }
            .input {
                flex: 1; padding: 8px 12px; border: 1px solid rgba(255,255,255,.08);
                border-radius: 8px; background: rgba(255,255,255,.04); color: #e4e4e7;
                font-size: 13px; outline: none; font-family: inherit;
            }
            .input:focus { border-color: rgba(16,185,129,.3); }
            .submit-btn {
                padding: 8px 16px; border: none; border-radius: 8px;
                background: #e4e4e7; color: #09090b; font-size: 12px; font-weight: 600;
                cursor: pointer; font-family: inherit;
            }
            .feedback { font-size: 12px; margin-top: 10px; }
            .correct { color: #10b981; }
            .incorrect { color: #ef4444; }
            .skip { display: block; color: #3f3f46; font-size: 11px; margin-top: 12px; cursor: pointer; background: none; border: none; text-align: center; width: 100%; font-family: inherit; }
            .skip:hover { color: #71717a; }
        </style>
        <div class="scrim" id="scrim">
            <div class="card">
                <div class="title">${headline}</div>
                <div class="question">${question}</div>
                <div class="input-row">
                    <input class="input" id="recall-input" placeholder="Your answer..." autofocus />
                    <button class="submit-btn" id="submit-btn">Check</button>
                </div>
                <div class="feedback" id="feedback"></div>
                <button class="skip" id="skip-btn">Skip</button>
            </div>
        </div>
    `;

    document.body.appendChild(host);

    const input = shadow.getElementById("recall-input") as HTMLInputElement;
    const submitBtn = shadow.getElementById("submit-btn");
    const feedback = shadow.getElementById("feedback");
    const skipBtn = shadow.getElementById("skip-btn");

    const checkAnswer = () => {
        if (!input || !feedback) return;
        const userAnswer = input.value.trim().toLowerCase();
        const correct = answer.toLowerCase();
        if (userAnswer && (correct.includes(userAnswer) || userAnswer.includes(correct))) {
            feedback.className = "feedback correct";
            feedback.textContent = "Correct! Unblurring...";
            sendUserAction("engaged", String(payload.intervention_id || ""));
            setTimeout(removeOverlay, 1000);
        } else {
            feedback.className = "feedback incorrect";
            feedback.textContent = "Not quite. The answer is: " + answer;
            setTimeout(removeOverlay, 3000);
        }
    };

    submitBtn?.addEventListener("click", checkAnswer);
    input?.addEventListener("keydown", (e: KeyboardEvent) => {
        if (e.key === "Enter") checkAnswer();
    });
    skipBtn?.addEventListener("click", () => {
        sendUserAction("dismissed", String(payload.intervention_id || ""));
        removeOverlay();
    });
}

// --- Resume Card ---

const CORTEX_RESUME_ID = "cortex-resume-card";
let resumeAutoDismissTimer: ReturnType<typeof setTimeout> | null = null;

function removeResumeCard(): void {
    const el = document.getElementById(CORTEX_RESUME_ID);
    if (el) el.remove();
    if (resumeAutoDismissTimer) { clearTimeout(resumeAutoDismissTimer); resumeAutoDismissTimer = null; }
}

function fmtTime(seconds: number): string {
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    return `${m}:${String(sec).padStart(2, "0")}`;
}

function timeAgo(epochMs: number): string {
    const diff = Date.now() - epochMs;
    const mins = Math.floor(diff / 60000);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
}

function getPositionDisplay(pos: Record<string, unknown>): { label: string; pct: number } {
    switch (pos.type) {
        case "video": {
            const ts = pos.timestamp_s as number;
            const dur = pos.duration_s as number;
            const pct = dur > 0 ? (ts / dur) * 100 : 0;
            return { label: `▶ ${fmtTime(ts)} / ${fmtTime(dur)}`, pct };
        }
        case "scroll":
            return { label: `📄 ${Math.round(pos.scroll_pct as number)}% read`, pct: pos.max_scroll_pct as number };
        case "code_problem":
            return {
                label: `Stage: ${pos.stage} · ${pos.wrong_answer_count} WA · ${Math.round((pos.time_elapsed_s as number) / 60)} min`,
                pct: Math.min((pos.time_elapsed_s as number) / 1800 * 100, 100),
            };
        case "notebook":
            return { label: `Cell ${(pos.cell_index as number) + 1} · ${Math.round(pos.scroll_pct as number)}% scrolled`, pct: pos.scroll_pct as number };
        case "pdf": {
            const total = pos.total_pages as number;
            const pct = total > 0 ? ((pos.page as number) / total) * 100 : 0;
            return { label: `Page ${pos.page} / ${total || "?"}`, pct };
        }
        case "slides": {
            const total = pos.total_slides as number;
            const pct = total > 0 ? (((pos.slide_index as number) + 1) / total) * 100 : 0;
            return { label: `Slide ${(pos.slide_index as number) + 1} / ${total || "?"}`, pct };
        }
        case "general":
            return { label: `📄 ${Math.round(pos.scroll_pct as number)}% scrolled`, pct: pos.max_scroll_pct as number || pos.scroll_pct as number };
        default:
            return { label: "", pct: 0 };
    }
}

function showResumeCard(activity: Record<string, unknown>): void {
    removeResumeCard();

    const pos = activity.position as Record<string, unknown>;
    const { label: posLabel, pct: progressPct } = getPositionDisplay(pos);
    const platform = (activity.platform as string) || "";
    const title = (activity.title as string) || "";
    const chapter = pos.type === "video" && pos.chapter ? pos.chapter as string : "";
    const lastVisited = activity.last_visited as number;

    const host = document.createElement("div");
    host.id = CORTEX_RESUME_ID;
    host.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483646;pointer-events:none;";

    const shadow = host.attachShadow({ mode: "closed" });

    const platformLabel = escapeHtml(platform.charAt(0).toUpperCase() + platform.slice(1));
    const safeTitle = escapeHtml(title);
    const chapterHtml = chapter ? `<div class="chapter">${escapeHtml(chapter)}</div>` : "";
    const barWidth = Math.min(100, Math.max(0, progressPct));

    shadow.innerHTML = `
        <style>
            @keyframes panelIn {
                from { transform: translateY(12px) scale(.99); opacity: 0; }
                to   { transform: translateY(0) scale(1); opacity: 1; }
            }
            * { box-sizing: border-box; margin: 0; padding: 0; }
            .card {
                position: fixed; bottom: 20px; right: 20px; width: 300px;
                pointer-events: auto;
                background: #111113; border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.06);
                box-shadow: 0 0 0 .5px rgba(0,0,0,.3), 0 4px 20px rgba(0,0,0,.4), 0 16px 40px rgba(0,0,0,.2);
                font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'SF Pro Text', system-ui, sans-serif;
                animation: panelIn 0.3s cubic-bezier(0.16, 1, 0.3, 1);
                color: #e4e4e7;
                padding: 16px;
            }
            .top-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
            .platform { font-size: 11px; color: #71717a; letter-spacing: 0.3px; }
            .close-btn {
                width: 20px; height: 20px; border: none; background: rgba(255,255,255,.04);
                border-radius: 5px; cursor: pointer; display: flex; align-items: center;
                justify-content: center; transition: background .12s;
            }
            .close-btn:hover { background: rgba(255,255,255,.08); }
            .close-btn svg { width: 8px; height: 8px; stroke: #71717a; stroke-width: 2; }
            .title { font-size: 13px; font-weight: 600; letter-spacing: -0.2px; line-height: 1.4; margin-bottom: 2px; color: #e4e4e7; }
            .chapter { font-size: 11px; color: #52525b; margin-bottom: 6px; }
            .position { font-size: 12px; color: #a1a1aa; font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; margin: 8px 0 6px; }
            .bar-bg { width: 100%; height: 3px; background: rgba(255,255,255,.06); border-radius: 2px; margin-bottom: 4px; }
            .bar-fill { height: 100%; border-radius: 2px; background: #10b981; transition: width .3s; }
            .pct { font-size: 10px; color: #52525b; text-align: right; margin-bottom: 12px; }
            .resume-btn {
                display: block; width: 100%; padding: 8px; border: none; border-radius: 8px;
                background: rgba(16, 185, 129, 0.12); color: #10b981; cursor: pointer;
                font-size: 12px; font-weight: 600; font-family: inherit; letter-spacing: 0.2px;
                transition: background .12s;
            }
            .resume-btn:hover { background: rgba(16, 185, 129, 0.2); }
            .dismiss-btn {
                display: block; width: 100%; padding: 5px; margin-top: 4px;
                border: none; background: none; color: #3f3f46; cursor: pointer;
                font-size: 11px; font-family: inherit; transition: color .12s;
            }
            .dismiss-btn:hover { color: #71717a; }
        </style>
        <div class="card" id="resume-card">
            <div class="top-row">
                <span class="platform">${platformLabel} · ${timeAgo(lastVisited)}</span>
                <button class="close-btn" id="resume-close">
                    <svg viewBox="0 0 10 10" fill="none"><path d="M1 1l8 8M9 1l-8 8"/></svg>
                </button>
            </div>
            <div class="title">${safeTitle}</div>
            ${chapterHtml}
            <div class="position">${escapeHtml(posLabel)}</div>
            <div class="bar-bg"><div class="bar-fill" style="width:${barWidth}%"></div></div>
            <div class="pct">${Math.round(progressPct)}%</div>
            <button class="resume-btn" id="resume-action">Resume ▸</button>
            <button class="dismiss-btn" id="resume-dismiss">Dismiss</button>
        </div>
    `;

    document.body.appendChild(host);

    // Wire up buttons
    const closeBtn = shadow.getElementById("resume-close");
    const resumeBtn = shadow.getElementById("resume-action");
    const dismissBtn = shadow.getElementById("resume-dismiss");

    closeBtn?.addEventListener("click", () => removeResumeCard());

    resumeBtn?.addEventListener("click", () => {
        executeResume(activity);
        removeResumeCard();
    });

    dismissBtn?.addEventListener("click", () => {
        removeResumeCard();
        try {
            chrome.runtime.sendMessage({
                type: "DISMISS_RESUME",
                content_id: activity.content_id,
            }).catch(() => {});
        } catch { /* context invalidated */ }
    });

    // ESC dismisses
    const escHandler = (e: KeyboardEvent) => {
        if (e.key === "Escape") { removeResumeCard(); document.removeEventListener("keydown", escHandler); }
    };
    document.addEventListener("keydown", escHandler);

    // Auto-dismiss after 15s
    resumeAutoDismissTimer = setTimeout(removeResumeCard, 15000);

    // If user scrolls, fade out sooner
    let scrollDismissTimer: ReturnType<typeof setTimeout> | null = null;
    const scrollHandler = () => {
        if (!scrollDismissTimer) {
            scrollDismissTimer = setTimeout(removeResumeCard, 5000);
        }
    };
    window.addEventListener("scroll", scrollHandler, { once: true });
}

function executeResume(activity: Record<string, unknown>): void {
    const pos = activity.position as Record<string, unknown>;

    switch (pos.type) {
        case "video": {
            const targetTime = pos.timestamp_s as number;
            const savedDuration = activity.content_duration_s as number;

            // Wait for video element, then seek
            const trySeek = (video: HTMLVideoElement) => {
                // Verify same content: duration within 5s tolerance
                if (savedDuration > 0 && Math.abs(video.duration - savedDuration) > 5) {
                    showResumeToast("Different video detected", `Your saved position was ${fmtTime(targetTime)}`);
                    return;
                }
                video.currentTime = targetTime;
                video.play().catch(() => {}); // Some browsers block autoplay
                showResumeToast("Resumed", `Jumped to ${fmtTime(targetTime)}`);
            };

            // Try to find video now; if not found, use MutationObserver
            const selectors = ["video.html5-main-video", ".bpx-player-video-wrap video", "video"];
            let found = false;
            for (const sel of selectors) {
                const v = document.querySelector<HTMLVideoElement>(sel);
                if (v && v.readyState >= 1) { trySeek(v); found = true; break; }
            }
            if (!found) {
                // Wait for video to appear
                const observer = new MutationObserver(() => {
                    for (const sel of selectors) {
                        const v = document.querySelector<HTMLVideoElement>(sel);
                        if (v) {
                            observer.disconnect();
                            clearTimeout(timeout);
                            if (v.readyState >= 1) { trySeek(v); }
                            else { v.addEventListener("loadedmetadata", () => trySeek(v), { once: true }); }
                            return;
                        }
                    }
                });
                observer.observe(document.body, { childList: true, subtree: true });
                const timeout = setTimeout(() => {
                    observer.disconnect();
                    showResumeToast("Video not loaded", `Your position was ${fmtTime(targetTime)}`);
                }, 15000);
            }
            break;
        }
        case "scroll": {
            const px = pos.scroll_px as number;
            window.scrollTo({ top: px, behavior: "smooth" });
            showResumeToast("Resumed", `Scrolled to ${Math.round(pos.scroll_pct as number)}%`);
            break;
        }
        case "code_problem": {
            if (pos.code_snapshot) {
                const editor = document.querySelector(".monaco-editor") as any;
                if (editor) {
                    const monacoInstance = editor?.__vue__?.$refs?.monaco?.getEditor?.();
                    if (monacoInstance) {
                        const currentCode = monacoInstance.getValue();
                        if (!currentCode || currentCode.trim().length < 20) {
                            monacoInstance.setValue(pos.code_snapshot as string);
                            showResumeToast("Code restored", "Your previous code has been pasted");
                        }
                    }
                }
            }
            showResumeToast("Welcome back", `You were in ${pos.stage} stage · ${pos.wrong_answer_count} WA`);
            break;
        }
        case "notebook": {
            const cells = document.querySelectorAll("colab-cell, .cell, .jp-Cell");
            const idx = pos.cell_index as number;
            if (cells[idx]) {
                cells[idx].scrollIntoView({ behavior: "smooth" });
                showResumeToast("Resumed", `Scrolled to cell ${idx + 1}`);
            }
            break;
        }
        case "pdf": {
            const pdfApp = (window as any).PDFViewerApplication;
            if (pdfApp) {
                pdfApp.page = pos.page as number;
            } else {
                location.hash = `#page=${pos.page}`;
            }
            showResumeToast("Resumed", `Jumped to page ${pos.page}/${pos.total_pages}`);
            break;
        }
        case "slides": {
            const reveal = (window as any).Reveal;
            if (reveal) {
                reveal.slide(pos.slide_index as number);
            } else {
                location.hash = `#slide=id.p${pos.slide_index}`;
            }
            showResumeToast("Resumed", `Jumped to slide ${(pos.slide_index as number) + 1}`);
            break;
        }
        case "general": {
            const px = (pos as any).scroll_px ?? 0;
            window.scrollTo({ top: px, behavior: "smooth" });
            break;
        }
    }
}

function showResumeToast(title: string, body: string): void {
    const id = "cortex-resume-toast";
    document.getElementById(id)?.remove();
    const el = document.createElement("div");
    el.id = id;
    el.style.cssText =
        "position:fixed;top:16px;right:16px;z-index:2147483647;max-width:280px;" +
        "padding:10px 14px;border-radius:10px;font-family:-apple-system,BlinkMacSystemFont,'Inter',system-ui,sans-serif;" +
        "background:#111113;color:#e4e4e7;border:1px solid rgba(255,255,255,.06);" +
        "box-shadow:0 4px 20px rgba(0,0,0,.4);animation:cortexSlideIn .25s ease;font-size:12px;line-height:1.5;" +
        "cursor:pointer;";
    el.innerHTML =
        `<style>@keyframes cortexSlideIn{from{transform:translateY(-12px);opacity:0}to{transform:translateY(0);opacity:1}}</style>` +
        `<div style="font-weight:600;margin-bottom:2px;font-size:12px;color:#10b981">${escapeHtml(title)}</div>` +
        `<div style="color:#71717a;font-size:11px">${escapeHtml(body)}</div>`;
    el.addEventListener("click", () => el.remove());
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 5000);
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

            case "SHOW_BREATHING_OVERLAY":
                showBreathingOverlay(message.payload as Record<string, unknown>);
                sendResponse({ ok: true });
                break;

            case "SHOW_ACTIVE_RECALL":
                showActiveRecall(message.payload as Record<string, unknown>);
                sendResponse({ ok: true });
                break;

            case "AMBIENT_STATE_UPDATE": {
                const payload = message.payload as Record<string, unknown>;
                updateAmbient(payload);
                sendResponse({ ok: true });
                break;
            }

            case "SHOW_RESUME_CARD": {
                const activity = message.activity as Record<string, unknown>;
                if (activity) showResumeCard(activity);
                sendResponse({ ok: true });
                break;
            }
        }
        return false;
    },
);

// Cleanup on unload
window.addEventListener("beforeunload", () => {
    if (particleRaf !== null) cancelAnimationFrame(particleRaf);
    restoreShieldedElements();
    ambientContainer?.remove();
});
