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
    opacity: number;
    life: number;
    maxLife: number;
    type: "mist" | "rain" | "warmth" | "calm";
}

function createParticle(type: Particle["type"], w: number, h: number): Particle {
    switch (type) {
        case "rain":
            return {
                x: Math.random() * w,
                y: -10,
                vx: -0.5 + Math.random() * -1,
                vy: 3 + Math.random() * 4,
                size: 1 + Math.random(),
                opacity: 0.03 + Math.random() * 0.04,
                life: 0,
                maxLife: h / 3,
                type,
            };
        case "mist":
            return {
                x: Math.random() * w,
                y: Math.random() * h,
                vx: (Math.random() - 0.5) * 0.3,
                vy: (Math.random() - 0.5) * 0.2,
                size: 30 + Math.random() * 60,
                opacity: 0.01 + Math.random() * 0.015,
                life: 0,
                maxLife: 300 + Math.random() * 200,
                type,
            };
        case "warmth":
            return {
                x: Math.random() * w,
                y: h + 10,
                vx: (Math.random() - 0.5) * 0.5,
                vy: -0.3 - Math.random() * 0.5,
                size: 3 + Math.random() * 4,
                opacity: 0.02 + Math.random() * 0.03,
                life: 0,
                maxLife: 200 + Math.random() * 150,
                type,
            };
        case "calm":
        default:
            return {
                x: Math.random() * w,
                y: Math.random() * h,
                vx: (Math.random() - 0.5) * 0.1,
                vy: -0.1 - Math.random() * 0.1,
                size: 2 + Math.random() * 3,
                opacity: 0.015 + Math.random() * 0.02,
                life: 0,
                maxLife: 400 + Math.random() * 200,
                type,
            };
    }
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
        transition: background 60s ease, box-shadow 60s ease;
        pointer-events:none;
    `;
    host.appendChild(auraElement);

    // 2. Somatic Filter — color temperature overlay
    filterElement = document.createElement("div");
    filterElement.style.cssText = `
        position:absolute;top:0;left:0;right:0;bottom:0;
        mix-blend-mode:color;
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
    const intensity = 0.03 + Math.max(flowVal, stressVal) * 0.04; // 3-7% max

    auraElement.style.boxShadow =
        `inset 0 0 120px 40px hsla(${hue}, ${saturation}%, ${lightness}%, ${intensity}),` +
        `inset 0 0 300px 100px hsla(${hue}, ${saturation}%, ${lightness}%, ${intensity * 0.4})`;
}

// --- Layer 2: Somatic Filter ---

function updateFilter(): void {
    if (!filterElement) return;

    // Color temperature shift:
    // Low HRV / high stress → cooler, desaturated (institutional feel)
    // High HRV / flow → warmer, richer (golden afternoon)
    const flowVal = smoothedFlow;
    const stressVal = smoothedStress;

    if (flowVal > 0.3 || stressVal > 0.3) {
        const warmth = flowVal - stressVal; // -1 to +1
        // Warm: rgba(255, 200, 100, 0.03), Cool: rgba(100, 140, 220, 0.03)
        const r = Math.round(warmth > 0 ? 255 : 100 + (1 + warmth) * 77);
        const g = Math.round(warmth > 0 ? 200 - warmth * 40 : 140 + (1 + warmth) * 30);
        const b = Math.round(warmth > 0 ? 100 : 220 - (1 + warmth) * 40);
        const opacity = Math.min(0.04, Math.abs(warmth) * 0.05);

        filterElement.style.backgroundColor = `rgba(${r}, ${g}, ${b}, ${opacity})`;
        filterElement.style.opacity = "1";
    } else {
        filterElement.style.opacity = "0";
    }
}

// --- Layer 3: Biometric Weather ---

function updateWeather(): void {
    if (!weatherCanvas || !weatherCtx) return;

    const w = weatherCanvas.width;
    const h = weatherCanvas.height;
    const ctx = weatherCtx;

    ctx.clearRect(0, 0, w, h);

    const stressVal = smoothedStress;
    const flowVal = smoothedFlow;
    const state = currentState?.state ?? "FLOW";

    // Determine weather type and particle target count
    let targetType: Particle["type"] = "calm";
    let targetCount = 8;

    if (stressVal > 0.6) {
        targetType = "rain";
        targetCount = 20 + Math.round(stressVal * 25);
    } else if (stressVal > 0.35) {
        targetType = "mist";
        targetCount = 12 + Math.round(stressVal * 15);
    } else if (flowVal > 0.4) {
        targetType = "warmth";
        targetCount = 10 + Math.round(flowVal * 12);
    } else if (state === "RECOVERY") {
        targetType = "warmth";
        targetCount = 8;
    } else {
        targetType = "calm";
        targetCount = 6;
    }

    // Spawn particles gradually
    if (particles.length < targetCount && Math.random() < 0.3) {
        particles.push(createParticle(targetType, w, h));
    }

    // Update and draw particles
    particles = particles.filter((p) => {
        p.life++;
        if (p.life > p.maxLife) return false;

        p.x += p.vx;
        p.y += p.vy;

        // Fade in/out
        const lifeFrac = p.life / p.maxLife;
        const fadeAlpha =
            lifeFrac < 0.1 ? lifeFrac / 0.1 :
            lifeFrac > 0.8 ? (1 - lifeFrac) / 0.2 : 1;
        const alpha = p.opacity * fadeAlpha;

        if (p.x < -50 || p.x > w + 50 || p.y < -50 || p.y > h + 50) return false;

        ctx.globalAlpha = alpha;

        switch (p.type) {
            case "rain": {
                // Thin diagonal streaks
                ctx.strokeStyle = "rgba(150, 180, 220, 1)";
                ctx.lineWidth = p.size * 0.5;
                ctx.beginPath();
                ctx.moveTo(p.x, p.y);
                ctx.lineTo(p.x + p.vx * 3, p.y + p.vy * 3);
                ctx.stroke();
                break;
            }
            case "mist": {
                // Soft blurred circles
                const gradient = ctx.createRadialGradient(
                    p.x, p.y, 0, p.x, p.y, p.size,
                );
                gradient.addColorStop(0, "rgba(180, 200, 220, 0.8)");
                gradient.addColorStop(1, "rgba(180, 200, 220, 0)");
                ctx.fillStyle = gradient;
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                ctx.fill();
                break;
            }
            case "warmth": {
                // Golden floating particles rising upward
                const grad = ctx.createRadialGradient(
                    p.x, p.y, 0, p.x, p.y, p.size,
                );
                grad.addColorStop(0, "rgba(255, 200, 100, 0.9)");
                grad.addColorStop(0.5, "rgba(255, 160, 60, 0.4)");
                grad.addColorStop(1, "rgba(255, 140, 40, 0)");
                ctx.fillStyle = grad;
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                ctx.fill();
                break;
            }
            case "calm": {
                // Gentle drifting motes
                const cg = ctx.createRadialGradient(
                    p.x, p.y, 0, p.x, p.y, p.size,
                );
                cg.addColorStop(0, "rgba(150, 200, 255, 0.7)");
                cg.addColorStop(1, "rgba(150, 200, 255, 0)");
                ctx.fillStyle = cg;
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                ctx.fill();
                break;
            }
        }
        return true;
    });

    ctx.globalAlpha = 1;
}

// --- Layer 4: Flow Shield ---

function updateFlowShield(): void {
    if (!currentState) return;

    const flowVal = currentState.scores?.flow ?? 0;
    const dwell = currentState.dwell_seconds ?? 0;
    const state = currentState.state;

    // Activate flow shield after 120s of sustained FLOW state
    const shouldShield = state === "FLOW" && dwell > 120 && flowVal > 0.4;

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
        htmlEl.style.transition = "opacity 60s ease-in, filter 60s ease-in";
        htmlEl.style.opacity = "";
        htmlEl.style.filter = "";
        // Restore original styles after transition
        setTimeout(() => {
            htmlEl.style.cssText = originalStyle;
        }, 62000);
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
