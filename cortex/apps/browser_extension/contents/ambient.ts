/**
 * Cortex Ambient Engine — Content Script
 *
 * Creates a living, breathing browser environment that responds to your
 * biometric state in real-time. No popups, no notifications — the page
 * itself becomes a mirror of your body.
 *
 * Layers:
 * 1. Ambient Aura — A soft glowing vignette around the viewport
 * 2. Somatic Filter — Subtle color temperature shifts based on HRV
 * 3. Flow Shield — Dissolves distracting elements during deep focus
 * 4. Biometric Weather — Atmospheric particles reflecting internal state
 *
 * All effects are sub-threshold: <2% change per minute, registered
 * somatically but not consciously. Based on Calm Technology principles.
 *
 * @plasmo content_script
 */

import type { PlasmoCSConfig } from "plasmo";

export const config: PlasmoCSConfig = {
    matches: ["https://*/*", "http://*/*"],
    run_at: "document_idle",
};

// --- Types ---

interface CortexBiometrics {
    heart_rate: number | null;
    hrv_rmssd: number | null;
    hr_delta: number | null;
    blink_rate: number | null;
    forward_lean: number | null;
}

interface CortexAmbientState {
    state: string;
    confidence: number;
    scores: Record<string, number>;
    dwell_seconds: number;
    biometrics?: CortexBiometrics;
}

// --- Constants ---

const AMBIENT_HOST_ID = "cortex-ambient-engine";

// Flow shield: CSS selectors for common distracting elements
const DISTRACTION_SELECTORS: Record<string, string[]> = {
    "github.com": [
        '[aria-label="Explore"]',
        ".feed-right-sidebar",
        ".dashboard-sidebar",
        '[data-testid="pinned-items-reorder-button"]',
    ],
    "twitter.com": [
        '[data-testid="sidebarColumn"]',
        '[aria-label="Trending"]',
        '[aria-label="Who to follow"]',
    ],
    "x.com": [
        '[data-testid="sidebarColumn"]',
        '[aria-label="Trending"]',
        '[aria-label="Who to follow"]',
    ],
    "youtube.com": [
        "#related",
        "#comments",
        "ytd-mini-guide-renderer",
        "#guide",
        "ytd-merch-shelf-renderer",
    ],
    "reddit.com": [
        '[data-testid="frontpage-sidebar"]',
        "aside",
        ".promotedlink",
    ],
    "news.ycombinator.com": [".pagetop"],
    "*": [
        // Universal distractors
        '[class*="cookie"]',
        '[class*="Cookie"]',
        '[id*="cookie"]',
        '[class*="newsletter"]',
        '[class*="popup"]',
        '[class*="chat-widget"]',
        '[class*="intercom"]',
        '[id*="intercom"]',
        '[class*="drift"]',
        '[id*="hubspot"]',
    ],
};

// --- State ---

let currentState: CortexAmbientState | null = null;
let auraElement: HTMLDivElement | null = null;
let filterElement: HTMLDivElement | null = null;
let weatherCanvas: HTMLCanvasElement | null = null;
let weatherCtx: CanvasRenderingContext2D | null = null;
let particles: Particle[] = [];
let flowShieldActive = false;
let flowDwellStart = 0;
let animFrameId: number | null = null;
let shieldedElements: Map<Element, string> = new Map();

// Smoothed values (for slow transitions)
let smoothedStress = 0;
let smoothedFlow = 0;
let smoothedHeartRate = 72;
let lastUpdateTime = 0;

// --- Particle System ---

interface Particle {
    x: number;
    y: number;
    vx: number;
    vy: number;
    size: number;
    life: number;
    maxLife: number;
}

function createParticle(_type: string, w: number, h: number): Particle {
    // Gentle floating dots — slow drift, long life
    return {
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.1,
        vy: -0.1 - Math.random() * 0.1,
        size: 2 + Math.random() * 3,
        life: 0,
        maxLife: 400 + Math.random() * 200,
    };
}

// --- Initialization ---

function init(): void {
    if (document.getElementById(AMBIENT_HOST_ID)) return;

    // Create ambient host (fixed, pointer-events:none, highest z-index)
    const host = document.createElement("div");
    host.id = AMBIENT_HOST_ID;
    host.style.cssText =
        "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483646;pointer-events:none;overflow:hidden;";

    // 1. Ambient Aura — radial gradient vignette
    auraElement = document.createElement("div");
    auraElement.style.cssText = `
        position:absolute;top:0;left:0;right:0;bottom:0;
        transition: background 3s ease, box-shadow 3s ease;
        pointer-events:none;
    `;
    host.appendChild(auraElement);

    // 2. Somatic Filter — color temperature overlay
    filterElement = document.createElement("div");
    filterElement.style.cssText = `
        position:absolute;top:0;left:0;right:0;bottom:0;
        mix-blend-mode:multiply;
        transition: background-color 45s ease;
        opacity:0;
        pointer-events:none;
    `;
    host.appendChild(filterElement);

    // 3. Weather Canvas — atmospheric particles
    weatherCanvas = document.createElement("canvas");
    weatherCanvas.style.cssText =
        "position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;";
    weatherCanvas.width = window.innerWidth;
    weatherCanvas.height = window.innerHeight;
    weatherCtx = weatherCanvas.getContext("2d", { alpha: true });
    host.appendChild(weatherCanvas);

    document.body.appendChild(host);

    // Handle resize
    window.addEventListener("resize", () => {
        if (weatherCanvas) {
            weatherCanvas.width = window.innerWidth;
            weatherCanvas.height = window.innerHeight;
        }
    });

    // Start animation loop
    lastUpdateTime = performance.now();
    animFrameId = requestAnimationFrame(tick);
}

// --- Main Animation Loop (targets ~15fps for efficiency) ---

let lastFrameTime = 0;
const FRAME_INTERVAL = 1000 / 15; // 15fps

function tick(now: number): void {
    animFrameId = requestAnimationFrame(tick);

    if (now - lastFrameTime < FRAME_INTERVAL) return;
    lastFrameTime = now;

    if (!currentState) return;

    const dt = Math.min((now - lastUpdateTime) / 1000, 0.5);
    lastUpdateTime = now;

    // Smooth values with very slow EMA (60s time constant)
    const alpha = 1 - Math.exp(-dt / 60);
    const targetStress = (currentState.scores?.hyper ?? 0);
    const targetFlow = (currentState.scores?.flow ?? 0);
    const targetHR = currentState.biometrics?.heart_rate ?? 72;

    smoothedStress += (targetStress - smoothedStress) * alpha;
    smoothedFlow += (targetFlow - smoothedFlow) * alpha;
    smoothedHeartRate += (targetHR - smoothedHeartRate) * alpha;

    updateAura();
    updateFilter();
    updateWeather();
    updateFlowShield();
}

// --- Layer 1: Ambient Aura ---

function updateAura(): void {
    if (!auraElement) return;

    // Map state to color
    // FLOW: soft blue-green (calm, creative)
    // HYPER: warm amber (alert, warning)
    // RECOVERY: gentle gold (healing)
    // HYPO: cool grey-blue (low energy)
    const flowVal = smoothedFlow;
    const stressVal = smoothedStress;

    // Compute hue: flow = 180 (cyan), stress = 30 (amber), neutral = 220 (blue)
    const hue = flowVal > stressVal
        ? 180 - (flowVal * 30) // Flow: 180 → 150 (cyan to sea-green)
        : 30 + (1 - stressVal) * 20; // Stress: 30 → 50 (amber to gold)

    const saturation = 40 + Math.max(flowVal, stressVal) * 30;
    const lightness = 50 + flowVal * 10;
    const intensity = Math.min(0.02, 0.01 + Math.max(flowVal, stressVal) * 0.01); // max 2% at edge

    auraElement.style.boxShadow =
        `inset 0 0 120px 40px hsla(${hue}, ${saturation}%, ${lightness}%, ${intensity}),` +
        `inset 0 0 300px 100px hsla(${hue}, ${saturation}%, ${lightness}%, ${intensity * 0.3})`;
}

// --- Layer 2: Somatic Filter ---
// FLOW: cool blue tint, 1% opacity
// HYPER: warm amber, 2% opacity
// HYPO: neutral, 0% opacity (no tint when disengaged)
// Transition: 45s (set in CSS)

const SOMATIC_MAP: Record<string, { r: number; g: number; b: number; opacity: number }> = {
    FLOW: { r: 100, g: 180, b: 220, opacity: 0.01 },
    HYPER: { r: 249, g: 150, b: 80, opacity: 0.02 },
    HYPO: { r: 140, g: 160, b: 210, opacity: 0 },
    RECOVERY: { r: 180, g: 160, b: 220, opacity: 0.015 },
};

function updateFilter(): void {
    if (!filterElement) return;

    const state = currentState?.state ?? "";
    const temp = SOMATIC_MAP[state];

    if (temp && temp.opacity > 0) {
        filterElement.style.backgroundColor = `rgba(${temp.r}, ${temp.g}, ${temp.b}, 1)`;
        filterElement.style.opacity = String(temp.opacity);
    } else {
        filterElement.style.opacity = "0";
    }
}

// --- Layer 3: Weather Particles ---
// Just gentle floating dots. No rain streaks. No storm metaphors.
// FLOW: 4 dots, HYPER: 4 dots (same — Cortex gets quiet when stressed), HYPO: 2 dots

function updateWeather(): void {
    if (!weatherCanvas || !weatherCtx) return;

    const w = weatherCanvas.width;
    const h = weatherCanvas.height;
    const ctx = weatherCtx;

    ctx.clearRect(0, 0, w, h);

    const state = currentState?.state ?? "FLOW";

    // Target count per guide: FLOW 4, HYPER 4, HYPO 2, RECOVERY 4
    let targetCount = 4;
    if (state === "HYPO") targetCount = 2;

    // Spawn particles gradually
    while (particles.length < targetCount) {
        particles.push(createParticle("calm", w, h));
    }

    // Get state color for dots
    const STATE_COLORS_RGB: Record<string, { r: number; g: number; b: number }> = {
        FLOW: { r: 52, g: 211, b: 153 },
        HYPER: { r: 249, g: 115, b: 22 },
        HYPO: { r: 96, g: 165, b: 250 },
        RECOVERY: { r: 167, g: 139, b: 250 },
    };
    const col = STATE_COLORS_RGB[state] || STATE_COLORS_RGB.FLOW;

    // Update and draw — all gentle floating dots at 4% opacity
    particles = particles.filter((p) => {
        p.life++;
        if (p.life > p.maxLife) return false;

        p.x += p.vx;
        p.y += p.vy;

        // Wrap around
        if (p.x < -10) p.x = w + 10;
        if (p.x > w + 10) p.x = -10;
        if (p.y < -10) p.y = h + 10;
        if (p.y > h + 10) p.y = -10;

        // Fade in/out
        const lifeFrac = p.life / p.maxLife;
        const fadeAlpha =
            lifeFrac < 0.1 ? lifeFrac / 0.1 :
            lifeFrac > 0.8 ? (1 - lifeFrac) / 0.2 : 1;

        ctx.globalAlpha = 0.04 * fadeAlpha;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        ctx.fillStyle = `rgb(${col.r},${col.g},${col.b})`;
        ctx.fill();
        return true;
    });

    // Remove excess particles gradually
    while (particles.length > targetCount) {
        particles.pop();
    }

    ctx.globalAlpha = 1;
}

// --- Layer 4: Flow Shield ---

function updateFlowShield(): void {
    if (!currentState) return;

    const flowVal = currentState.scores?.flow ?? 0;
    const dwell = currentState.dwell_seconds ?? 0;
    const state = currentState.state;

    // Activate flow shield after 120s of sustained FLOW state
    const shouldShield = state === "FLOW" && dwell > 180 && flowVal > 0.4;

    if (shouldShield && !flowShieldActive) {
        flowShieldActive = true;
        flowDwellStart = performance.now();
        activateFlowShield();
    } else if (!shouldShield && flowShieldActive) {
        flowShieldActive = false;
        deactivateFlowShield();
    }
}

function activateFlowShield(): void {
    const hostname = window.location.hostname;
    const selectors = [
        ...(DISTRACTION_SELECTORS["*"] || []),
        ...(DISTRACTION_SELECTORS[hostname] || []),
    ];

    for (const selector of selectors) {
        try {
            const elements = document.querySelectorAll(selector);
            elements.forEach((el) => {
                const htmlEl = el as HTMLElement;
                if (!shieldedElements.has(el)) {
                    shieldedElements.set(el, htmlEl.style.cssText);
                    htmlEl.style.transition = "opacity 180s ease-out, filter 180s ease-out";
                    htmlEl.style.opacity = "0.05";
                    htmlEl.style.filter = "blur(2px)";
                }
            });
        } catch {
            // Invalid selector
        }
    }
}

function deactivateFlowShield(): void {
    shieldedElements.forEach((originalStyle, el) => {
        const htmlEl = el as HTMLElement;
        htmlEl.style.transition = "opacity 5s ease-in, filter 5s ease-in";
        htmlEl.style.opacity = "";
        htmlEl.style.filter = "";
        // Restore original styles after transition
        setTimeout(() => {
            htmlEl.style.cssText = originalStyle;
        }, 5500);
    });
    shieldedElements.clear();
}

// --- Message Listener ---

chrome.runtime.onMessage.addListener(
    (
        message: Record<string, unknown>,
        _sender: chrome.runtime.MessageSender,
        sendResponse: (response: unknown) => void,
    ) => {
        if (message.type === "AMBIENT_STATE_UPDATE") {
            currentState = message.payload as CortexAmbientState;
            sendResponse({ ok: true });
        }
        return false;
    },
);

// --- Bootstrap ---

if (document.readyState === "complete" || document.readyState === "interactive") {
    init();
} else {
    document.addEventListener("DOMContentLoaded", init);
}
