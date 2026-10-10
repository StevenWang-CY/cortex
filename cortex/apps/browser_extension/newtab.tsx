/**
 * Cortex Pulse Room — New Tab Page
 *
 * Decorative light follows the most recent usable camera pulse estimate.
 * This visualisation does not represent measured beat-to-beat timing.
 *
 * Inspired by Rafael Lozano-Hemmer's Pulse Room installation.
 */

import React, { useEffect, useRef, useState } from "react";
import "./page-reset.css";
import { CX, STATE_COLORS_RGB, STATE_LABELS, CX_KEYFRAMES } from "./design-tokens";
import { newCorrelationId } from "./lib/correlation";
import { LAUNCH_FAILED_STATUS, type CortexState } from "./lib/popup-view-model";
import { currentSignalView, stateAgeMs, statePresentationFields } from "./lib/state-presentation";
import { detectBrowser } from "./lib/browser";

export const PULSE_MOTION_PAUSED_KEY = "cortex_pulse_motion_paused";

/** Storage key shared with the popup's "Use the browser's default new tab". */
export const NEWTAB_DISABLED_KEY = "cortex_newtab_disabled";

// P2-9: debug flag for newtab page — silences console output in production.
const CORTEX_DEBUG: boolean = (() => {
    try {
        const ime = (import.meta as unknown as { env?: Record<string, unknown> }).env;
        if (ime && ime.CORTEX_DEBUG === "true") return true;
    } catch { /* import.meta not available */ }
    try {
        if (typeof process !== "undefined" && process.env && process.env.CORTEX_DEBUG === "true") {
            return true;
        }
    } catch { /* process not available */ }
    return false;
})();

// F19b: every newtab-initiated user click mints a cid so the trace is
// continuous from this page through the background and daemon logs.
function nt_sendWithCid(
    msg: Record<string, unknown>,
    cb?: (resp: unknown) => void,
): string {
    const correlation_id = newCorrelationId();
    const enriched = { ...msg, correlation_id };
    if (CORTEX_DEBUG) {
        console.debug(`cortex.newtab.send cid=${correlation_id} type=${String(msg.type)}`);
    }
    try {
        chrome.runtime.sendMessage(enriched, (response) => {
            const lastErr = (chrome as unknown as {
                runtime?: { lastError?: { message?: string } };
            }).runtime?.lastError;
            if (lastErr) {
                if (CORTEX_DEBUG) {
                    console.warn(
                        "[cortex.newtab] sendMessage",
                        String(msg.type ?? "?"),
                        lastErr.message,
                    );
                }
                cb?.({ ok: false });
                return;
            }
            if (cb) cb(response);
        });
    } catch {
        cb?.({ ok: false });
    }
    return correlation_id;
}

interface Ring {
    born: number;
    radius: number;
    maxRadius: number;
    opacity: number;
}

interface RecentActivity {
    content_id: string;
    platform: string;
    content_type: string;
    title: string;
    url: string;
    position: Record<string, unknown>;
    content_duration_s: number;
    duration_spent_s: number;
    last_visited: number;
    completion_pct: number;
    max_completion_pct: number;
    related_tabs: string[];
}

/** Translucent material that resolves in both appearances. */
const GLASS_BACKGROUND = "color-mix(in srgb, var(--cx-control-bg) 65%, transparent)";

function headlinePrefix(state: string): string {
    switch (state) {
        case "FLOW":
            return "Steady with ";
        case "HYPER":
            return "Supported by ";
        case "RECOVERY":
            return "Settling with ";
        default:
            return "Resting with ";
    }
}

/**
 * Minimal page shown when the user chose the browser's default new tab.
 * A ``chrome://`` page cannot be the target of a plain link from an
 * extension page, so the button asks the tabs API to navigate.
 */
function DefaultNewTabNotice({ onRestore }: { onRestore: () => void }): React.ReactElement {
    const [hint, setHint] = useState("");
    const browserName = detectBrowser() === "edge" ? "Edge" : "Chrome";
    const openDefault = () => {
        try {
            chrome.tabs.update({ url: detectBrowser() === "edge" ? "edge://newtab" : "chrome://new-tab-page" }, () => {
                const lastErr = (chrome as unknown as {
                    runtime?: { lastError?: { message?: string } };
                }).runtime?.lastError;
                if (lastErr) setHint("Open a new tab from the browser’s menu to use its default page.");
            });
        } catch {
            setHint("Open a new tab from the browser’s menu to use its default page.");
        }
    };
    return (
        <main
            data-testid="newtab-default-notice"
            style={{
                minHeight: "100vh",
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                gap: 12,
                background: CX.bg,
                color: CX.text,
                fontFamily: CX.font,
                padding: 24,
                textAlign: "center",
            }}
        >
            <div style={{ fontFamily: CX.fontBrand, fontStyle: "italic", fontSize: 28 }}>Cortex.</div>
            <p style={{ margin: 0, fontSize: 14, color: CX.textSecondary }}>
                The Pulse Room is off for new tabs.
            </p>
            <button
                type="button"
                onClick={openDefault}
                data-testid="newtab-open-default"
                style={{
                    marginTop: 8,
                    padding: "10px 20px",
                    borderRadius: CX.radiusFull,
                    border: `1px solid ${CX.borderEmphasis}`,
                    background: CX.surface,
                    color: CX.text,
                    fontSize: 13,
                    fontWeight: 500,
                    fontFamily: CX.font,
                    cursor: "pointer",
                }}
            >
                Open {browserName}’s default new tab
            </button>
            {hint && <p role="status" style={{ margin: 0, fontSize: 12, color: CX.textTertiary }}>{hint}</p>}
            <button
                type="button"
                onClick={onRestore}
                data-testid="newtab-restore"
                style={{
                    background: "none",
                    border: "none",
                    padding: 4,
                    color: CX.accentText,
                    fontSize: 12,
                    fontFamily: CX.font,
                    cursor: "pointer",
                    textDecoration: "underline",
                    textUnderlineOffset: 2,
                }}
            >
                Turn the Pulse Room back on
            </button>
        </main>
    );
}

function PulseRoom(): React.ReactElement {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const stateRef = useRef({
        heartRate: 0,
        state: "",
        confidence: 0,
        connected: false,
    });
    const animRef = useRef({
        lastBeatTime: 0,
        beatInterval: 1000,
        pulsePhase: 0,
        rings: [] as Ring[],
        breathPhase: 0,
        traceHistory: [] as number[],
        glowColor: "",
    });
    const canvasBackgroundRef = useRef<string>(CX.light.window_bg);

    const [sample, setSample] = useState<{
        state: CortexState | null; connected: boolean; age: number | null; receivedAt: number;
    }>({ state: null, connected: false, age: null, receivedAt: performance.now() });
    const [clock, setClock] = useState(performance.now());
    const [motionPaused, setMotionPaused] = useState(false);
    const signal = currentSignalView(sample.state, sample.connected,
        stateAgeMs(sample.age, sample.receivedAt, clock), STATE_LABELS);
    const displayHR = signal.heartRate;
    const displayConnected = sample.connected;
    const displayState = signal.stateKey;
    // The canvas reads the same projection that supplies visible text.
    stateRef.current = {
        heartRate: displayHR ?? 0, state: displayState, confidence: 0,
        connected: displayConnected,
    };
    if (displayHR) animRef.current.beatInterval = 60_000 / displayHR;
    else animRef.current.rings = [];
    const [newtabDisabled, setNewtabDisabled] = useState<boolean | null>(null);
    // Resolve before the first paint/effect pass so a reduced-motion user
    // never briefly schedules the ordinary canvas loop during hydration.
    const [reducedMotion, setReducedMotion] = useState(
        () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    );

    // Launch controls
    const [launching, setLaunching] = useState(false);
    const [launchError, setLaunchError] = useState("");
    const launchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const launchAttempt = useRef(0);
    useEffect(() => () => {
        launchAttempt.current++;
        if (launchTimer.current !== null) clearTimeout(launchTimer.current);
    }, []);

    // Activity tracking — resume cards at bottom
    const [activities, setActivities] = useState<RecentActivity[]>([]);
    const [showActivities, setShowActivities] = useState(false);

    // Honour the popup's "use the browser's default new tab" choice.
    useEffect(() => {
        let active = true;
        try {
            chrome.storage.local.get([NEWTAB_DISABLED_KEY, PULSE_MOTION_PAUSED_KEY], (result) => {
                if (!active) return;
                // Read lastError inside the callback so an unavailable store
                // cannot leave the first render waiting indefinitely.
                void chrome.runtime.lastError;
                setNewtabDisabled(result?.[NEWTAB_DISABLED_KEY] === true);
                setMotionPaused(result?.[PULSE_MOTION_PAUSED_KEY] === true);
            });
        } catch {
            setNewtabDisabled(false);
        }
        const onChanged = (
            changes: Record<string, { newValue?: unknown }>,
            area: string,
        ) => {
            if (area !== "local") return;
            if (changes[NEWTAB_DISABLED_KEY]) setNewtabDisabled(changes[NEWTAB_DISABLED_KEY].newValue === true);
            if (changes[PULSE_MOTION_PAUSED_KEY]) setMotionPaused(changes[PULSE_MOTION_PAUSED_KEY].newValue === true);
        };
        try {
            chrome.storage.onChanged.addListener(onChanged);
        } catch { /* storage events unavailable */ }
        return () => {
            active = false;
            try {
                chrome.storage.onChanged.removeListener(onChanged);
            } catch { /* ignore */ }
        };
    }, []);

    // Inject keyframes + interaction states
    useEffect(() => {
        const id = "cortex-newtab-styles";
        if (document.getElementById(id)) { return; }
        const style = document.createElement("style");
        style.id = id;
        style.textContent = `
            ${CX_KEYFRAMES}
            @keyframes activityFadeIn {
                from { opacity: 0; transform: translateY(8px); }
                to { opacity: 1; transform: translateY(0); }
            }
            .cortex-newtab-enter {
                animation: activityFadeIn 240ms ${CX.easeOut} both;
            }
            .cortex-launch-button {
                transition: background-color ${CX.durationFast} ${CX.easeOut},
                    border-color ${CX.durationFast} ${CX.easeOut},
                    box-shadow ${CX.durationFast} ${CX.easeOut},
                    opacity ${CX.durationFast} ${CX.easeOut},
                    transform ${CX.durationFast} ${CX.easeOut};
            }
            .cortex-launch-button:active:not(:disabled),
            .cortex-resume-card:active {
                transform: scale(0.97);
            }
            .cortex-launch-button:focus-visible,
            .cortex-resume-card:focus-visible {
                outline: 2px solid ${CX.accent};
                outline-offset: 3px;
            }
            .cortex-resume-card {
                opacity: 0;
                animation: activityFadeIn 240ms ${CX.easeOut} both;
                transition: transform ${CX.durationMicro} ${CX.easeOut},
                    box-shadow ${CX.durationFast} ${CX.easeOut};
            }
            .cortex-resume-item {
                flex: 0 0 220px;
                scroll-snap-align: start;
            }
            @media (hover: hover) and (pointer: fine) {
                .cortex-resume-card:hover {
                    transform: translateY(-4px);
                    box-shadow: ${CX.shadowFloat}, inset 0 0 0 1px var(--cx-border-emphasis) !important;
                }
            }
            .cortex-pulse-room { min-height: 100svh; display: flex; flex-direction: column; position: relative; overflow-x: hidden; }
            .cortex-pulse-header { position: relative; z-index: 10; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 24px 32px; }
            .cortex-motion-control { border: 1px solid var(--cx-border-emphasis); border-radius: ${CX.radiusFull}px; padding: 8px 14px; color: ${CX.textSecondary}; background: ${CX.surface}; font: inherit; font-size: 12px; cursor: pointer; min-height: 36px; }
            .cortex-motion-control { transition: background-color ${CX.durationFast} ${CX.easeOut}, color ${CX.durationFast} ${CX.easeOut}; }
            .cortex-motion-control:active { background: ${CX.surface}; }
            .cortex-motion-control:focus-visible { outline: 2px solid ${CX.accent}; outline-offset: 3px; }
            .cortex-pulse-main { flex: 1; display: grid; place-items: center; position: relative; z-index: 1; padding: 64px 24px 72px; min-height: 380px; }
            .cortex-pulse-caption { color: ${CX.textTertiary}; font-size: 12px; line-height: 1.6; margin: 18px 0 0; max-width: 320px; }
            .cortex-pulse-announcement { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
            .cortex-resume-section { position: relative; z-index: 1; width: min(100%, 840px); margin: 0 auto; padding: 0 32px 28px; }
            .cortex-resume-section h2 { margin: 0 4px 12px; color: ${CX.textSecondary}; font-size: 12px; font-weight: 500; }
            .cortex-resume-list { display: flex; gap: 16px; padding: 4px; overflow-x: auto; scroll-snap-type: x proximity; scrollbar-width: thin; }
            @media (max-width: 640px) {
                .cortex-pulse-header { padding: 16px 20px; }
                .cortex-pulse-main { padding: 48px 20px; }
                .cortex-resume-section { padding: 0 16px 20px; }
                .cortex-resume-list { gap: 12px; }
            }
            @media (max-height: 620px) { .cortex-pulse-main { padding: 32px 20px; min-height: 320px; } }
            @media (prefers-reduced-motion: reduce) {
                .cortex-launch-button:active { transform: none; }
                .cortex-newtab-enter,
                .cortex-resume-card {
                    animation: none !important;
                    opacity: 1 !important;
                    transform: none !important;
                }
            }
            @media (prefers-reduced-transparency: reduce) {
                .cortex-translucent-material {
                    background: var(--cx-control-bg) !important;
                    -webkit-backdrop-filter: none !important;
                    backdrop-filter: none !important;
                }
            }
        `;
        document.head.appendChild(style);

        const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
        setReducedMotion(mq.matches);
        const handler = (e: MediaQueryListEvent) => setReducedMotion(e.matches);
        mq.addEventListener("change", handler);
        const colorMq = window.matchMedia("(prefers-color-scheme: dark)");
        const syncCanvasTheme = (dark: boolean) => {
            canvasBackgroundRef.current = dark
                ? CX.dark.window_bg
                : CX.light.window_bg;
        };
        syncCanvasTheme(colorMq.matches);
        const colorHandler = (event: MediaQueryListEvent) =>
            syncCanvasTheme(event.matches);
        colorMq.addEventListener("change", colorHandler);

        return () => {
            document.head.removeChild(style);
            mq.removeEventListener("change", handler);
            colorMq.removeEventListener("change", colorHandler);
        };
    }, []);

    // Cached samples keep their original age. Polling the worker is not
    // evidence that the daemon produced another measurement.
    useEffect(() => {
        if (newtabDisabled !== false) return;
        let active = true;
        let interval: ReturnType<typeof setInterval> | null = null;
        const clear = () => {
            if (active) setSample({ state: null, connected: false, age: null, receivedAt: performance.now() });
        };
        const adopt = (raw: unknown, live: boolean) => {
            if (!active || !raw || typeof raw !== "object") return;
            const response = raw as Record<string, unknown>;
            const value = live ? response.payload : response.state;
            const state = value && typeof value === "object"
                ? { ...value as CortexState, ...statePresentationFields(value) } : null;
            const now = performance.now();
            setSample(previous => ({
                state, connected: live || response.connected === true,
                age: live ? 0 : typeof response.stateAgeMs === "number" && Number.isFinite(response.stateAgeMs)
                    ? Math.max(0, response.stateAgeMs)
                    : response.stateAgeMs === null ? null
                    : previous.state === null ? 0 : stateAgeMs(previous.age, previous.receivedAt, now),
                receivedAt: now,
            }));
            setClock(now);
        };
        const poll = () => {
            try {
                chrome.runtime.sendMessage({ type: "GET_STATE" }, (response) => {
                    if (chrome.runtime.lastError || !response) { clear(); return; }
                    adopt(response, false);
                });
            } catch { clear(); }
        };
        const listener = (message: Record<string, unknown>) => {
            if (message.type === "STATE_UPDATE") adopt(message, true);
            if (message.type === "CONNECTION_CHANGED") {
                if (message.connected !== true) clear();
                else poll();
            }
        };
        const sync = () => {
            if (interval !== null) clearInterval(interval);
            interval = null;
            if (document.visibilityState !== "visible") return;
            setClock(performance.now());
            poll();
            let tick = 0;
            interval = setInterval(() => {
                setClock(performance.now());
                if (++tick % 3 === 0) poll();
            }, 1000);
        };
        chrome.runtime.onMessage.addListener(listener);
        document.addEventListener("visibilitychange", sync);
        sync();
        return () => {
            active = false;
            if (interval !== null) clearInterval(interval);
            chrome.runtime.onMessage.removeListener(listener);
            document.removeEventListener("visibilitychange", sync);
        };
    }, [newtabDisabled]);

    // Fetch recent activities immediately; the cards' short entrance provides
    // visual continuity without an artificial data or interaction delay.
    useEffect(() => {
        if (newtabDisabled !== false) return;
        let active = true;
        try {
            chrome.runtime.sendMessage({ type: "GET_RECENT_ACTIVITIES", limit: 3 }, (result) => {
                if (chrome.runtime.lastError || !active || !Array.isArray(result)) return;
                setActivities(result);
                setShowActivities(result.length > 0);
            });
        } catch { /* extension context lost */ }
        return () => { active = false; };
    }, [newtabDisabled]);

    // Canvas animation loop + logo interaction
    const logoRef = useRef<HTMLDivElement>(null);
    const glowRef = useRef<HTMLDivElement>(null);
    const artRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (reducedMotion || motionPaused || newtabDisabled !== false) {
            animRef.current.rings = [];
            const canvas = canvasRef.current;
            canvas?.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
            if (logoRef.current) logoRef.current.style.transform = "scale(1)";
            if (glowRef.current) {
                glowRef.current.style.opacity = "0.35";
                glowRef.current.style.transform = "scale(1)";
            }
            return;
        }

        const canvas = canvasRef.current;
        if (!canvas) return;

        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        let rafId: number | null = null;
        let center = { x: window.innerWidth / 2, y: window.innerHeight / 2 };
        const locate = () => {
            const box = artRef.current?.getBoundingClientRect();
            if (box) center = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
        };

        function resize() {
            if (!canvas) return;
            const dpr = window.devicePixelRatio || 1;
            canvas.width = window.innerWidth * dpr;
            canvas.height = window.innerHeight * dpr;
            ctx!.scale(dpr, dpr);
            // Fill immediately on resize to prevent a flash of the page ground
            ctx!.fillStyle = canvasBackgroundRef.current;
            ctx!.fillRect(0, 0, window.innerWidth, window.innerHeight);
            locate();
        }
        resize();
        const resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(locate) : null;
        if (artRef.current) {
            resizeObserver?.observe(artRef.current);
            const main = artRef.current.closest("main");
            if (main) resizeObserver?.observe(main);
        }
        document.addEventListener("scroll", locate);
        window.addEventListener("resize", resize);

        function draw(now: number) {
            rafId = null;
            if (document.visibilityState !== "visible") return;
            if (!ctx || !canvas) return;

            const w = window.innerWidth;
            const h = window.innerHeight;
            const cx = center.x;
            const cy = center.y;

            const sr = stateRef.current;
            const anim = animRef.current;
            const col = STATE_COLORS_RGB[sr.state] || STATE_COLORS_RGB.FLOW;
            const isHyper = sr.state === "HYPER";

            ctx.clearRect(0, 0, w, h);
            ctx.fillStyle = canvasBackgroundRef.current;
            ctx.fillRect(0, 0, w, h);

            // Beat timing computed from the heartbeat
            if (sr.heartRate > 0) {
                const timeSinceBeat = now - anim.lastBeatTime;
                anim.pulsePhase = Math.min(timeSinceBeat / anim.beatInterval, 1);

                if (timeSinceBeat >= anim.beatInterval) {
                    anim.lastBeatTime = now;
                    anim.pulsePhase = 0;
                    if (!isHyper || anim.rings.length % 2 === 0) {
                        anim.rings.push({
                            born: now,
                            radius: 30,
                            maxRadius: 250 + Math.random() * 100,
                            opacity: 1.0,
                        });
                    }
                }
            } else {
                // Idle breathing
                anim.pulsePhase = (Math.sin(now / 1500) * 0.5 + 0.5);
            }

            // Ripple rings — delicate water ripples expanding outward
            anim.rings = anim.rings.filter((ring) => {
                const age = (now - ring.born) / 1000;
                if (age > 4) return false;

                ring.radius = ring.maxRadius * (1 - Math.exp(-age * 1.2));
                const fadeAlpha = Math.max(0, 0.4 * (1 - age / 4));

                ctx.beginPath();
                ctx.arc(cx, cy, ring.radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, ${fadeAlpha})`;
                ctx.lineWidth = 1;
                ctx.stroke();
                ctx.beginPath();
                ctx.arc(cx, cy, ring.radius * 0.8, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, ${fadeAlpha * 0.3})`;
                ctx.lineWidth = 0.5;
                ctx.stroke();

                return true;
            });

            // --- Drive the DOM elements: transform and opacity only ---
            const systole = sr.heartRate > 0
                ? (anim.pulsePhase < 0.15
                    ? Math.sin((anim.pulsePhase / 0.15) * (Math.PI / 2))
                    : Math.exp(-(anim.pulsePhase - 0.15) * 5))
                : anim.pulsePhase;

            const dampened = isHyper ? systole * 0.6 : systole;

            if (logoRef.current) {
                const scale = 1 + dampened * 0.08;
                logoRef.current.style.transform = `scale(${scale})`;
            }

            if (glowRef.current) {
                // The glow is pre-rendered once as a blurred radial layer; the
                // beat only retargets its opacity and scale (compositor-only).
                const glowColor = `rgb(${col.r}, ${col.g}, ${col.b})`;
                if (anim.glowColor !== glowColor) {
                    anim.glowColor = glowColor;
                    glowRef.current.style.background =
                        `radial-gradient(circle, rgba(${col.r}, ${col.g}, ${col.b}, 0.55) 0%, rgba(${col.r}, ${col.g}, ${col.b}, 0) 70%)`;
                }
                glowRef.current.style.opacity = String(0.25 + dampened * 0.75);
                glowRef.current.style.transform = `scale(${1 + dampened * 0.6})`;
            }

            rafId = requestAnimationFrame(draw);
        }

        function startAnimationLoop() {
            if (rafId !== null || document.visibilityState !== "visible") return;
            rafId = requestAnimationFrame(draw);
        }

        function stopAnimationLoop() {
            if (rafId === null) return;
            cancelAnimationFrame(rafId);
            rafId = null;
        }

        function syncAnimationLoop() {
            if (document.visibilityState === "visible") {
                startAnimationLoop();
            } else {
                stopAnimationLoop();
            }
        }

        document.addEventListener("visibilitychange", syncAnimationLoop);
        startAnimationLoop();

        return () => {
            stopAnimationLoop();
            document.removeEventListener("visibilitychange", syncAnimationLoop);
            window.removeEventListener("resize", resize);
            document.removeEventListener("scroll", locate);
            resizeObserver?.disconnect();
        };
    }, [reducedMotion, motionPaused, newtabDisabled]);

    function handleLaunch() {
        if (launching) return;
        const attempt = ++launchAttempt.current;
        setLaunching(true);
        setLaunchError("");
        const settle = (ok: boolean) => {
            if (attempt !== launchAttempt.current) return;
            launchAttempt.current++;
            if (launchTimer.current !== null) clearTimeout(launchTimer.current);
            launchTimer.current = null;
            setLaunching(false);
            setLaunchError(ok ? "" : LAUNCH_FAILED_STATUS);
        };
        launchTimer.current = setTimeout(() => settle(false), 45_000);
        nt_sendWithCid({ type: "LAUNCH_CORTEX" }, (raw: unknown) => {
            const resp = raw as { ok?: boolean } | undefined;
            settle(resp?.ok === true);
        });
    }

    const toggleMotion = () => {
        const paused = !motionPaused;
        setMotionPaused(paused);
        void chrome.storage.local.set({ [PULSE_MOTION_PAUSED_KEY]: paused }).catch(() => {});
    };

    if (newtabDisabled === true) {
        return (
            <DefaultNewTabNotice
                onRestore={() => {
                    try {
                        chrome.storage.local.set({ [NEWTAB_DISABLED_KEY]: false });
                    } catch { /* storage unavailable */ }
                    setNewtabDisabled(false);
                }}
            />
        );
    }

    const col = STATE_COLORS_RGB[displayConnected ? displayState : ""] || STATE_COLORS_RGB.FLOW;
    const stateLabel = signal.label;

    return (
        <div className="cortex-pulse-room" style={{ background: CX.bg, color: CX.text, fontFamily: CX.font }}>
            {/*
              CLAUDE.md rule 19: Plasmo's new-tab override page has a white
              <body> by default. The imported page-reset.css attaches with
              the React tree, so an inline <style> is emitted as the first
              child to paint the token ground during HTML parse, before React
              mounts; the canvas fillRect on resize is the third layer.
            */}
            <style
                dangerouslySetInnerHTML={{
                    __html:
                        "html,body,#__plasmo{background:" +
                        CX.bg +
                        ";margin:0;padding:0}",
                }}
            />
            <canvas ref={canvasRef} aria-hidden="true" style={{ position: "fixed", inset: 0, pointerEvents: "none", width: "100%", height: "100%" }} />
            <header className="cortex-pulse-header">
                <span style={{ fontSize: 12, color: CX.textSecondary, letterSpacing: "0.04em" }}>Pulse Room</span>
                {reducedMotion ? <span style={{ fontSize: 12, color: CX.textTertiary }}>Reduced motion</span> : (
                    <button className="cortex-motion-control" type="button" aria-pressed={motionPaused} onClick={toggleMotion}>
                        {motionPaused ? "Resume motion" : "Pause motion"}
                    </button>
                )}
            </header>

            {/* Static orb for reduced motion */}
            {(reducedMotion || motionPaused) && (
                <div style={{
                    position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                    background: `radial-gradient(circle at 50% 50%, rgba(${col.r},${col.g},${col.b},0.08), ${CX.bg})`,
                    zIndex: 0,
                }} />
            )}

            <main className="cortex-pulse-main">
                <div className="cortex-newtab-enter cortex-motion-enter" style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                    textAlign: "center",
                }}>
                <div ref={artRef} style={{ position: "relative", marginBottom: 32 }}>
                    {/* Pre-rendered glow: blurred once, animated via opacity/scale only */}
                    <div
                        ref={glowRef}
                        aria-hidden="true"
                        data-testid="pulse-glow"
                        style={{
                            position: "absolute",
                            inset: -60,
                            borderRadius: "50%",
                            filter: "blur(24px)",
                            opacity: 0.25,
                            transform: "scale(1)",
                            transformOrigin: "center center",
                            willChange: "opacity, transform",
                            pointerEvents: "none",
                            background: `radial-gradient(circle, rgba(${col.r}, ${col.g}, ${col.b}, 0.55) 0%, rgba(${col.r}, ${col.g}, ${col.b}, 0) 70%)`,
                        }}
                    />
                    <div
                        ref={logoRef}
                        style={{
                            position: "relative",
                            color: displayConnected ? `rgb(${col.r}, ${col.g}, ${col.b})` : CX.textSecondary,
                            willChange: "transform",
                            transformOrigin: "center center",
                        }}
                    >
                        <svg width="100" height="100" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                            <path d="M 51.8 12.2 A 28 28 0 1 0 51.8 51.8" fill="none" stroke={CX.text} strokeWidth="6" strokeLinecap="round" />
                            <path d="M 12 32 L 22 32 L 27 15 L 37 49 L 42 32 L 60 32" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
                            <circle cx="60" cy="32" r="3" fill="currentColor" />
                        </svg>
                    </div>
                </div>

                <h1 style={{
                    fontFamily: CX.fontSerif,
                    fontSize: "clamp(32px, 5vw, 48px)",
                    fontWeight: 400,
                    color: CX.text,
                    letterSpacing: "-0.02em",
                    margin: "0 0 16px 0",
                    userSelect: "none",
                }}>
                    {displayConnected ? (
                        <>
                            {headlinePrefix(displayState)}
                            <span style={{ fontFamily: CX.fontBrand, fontStyle: "italic", fontWeight: 500, paddingLeft: 4, letterSpacing: "0.02em" }}>Cortex.</span>
                        </>
                    ) : (
                        <span style={{ fontFamily: CX.fontBrand, fontStyle: "italic", fontWeight: 500, letterSpacing: "0.02em" }}>Cortex.</span>
                    )}
                </h1>

                {displayConnected ? (
                    <div
                        data-testid="newtab-state-line"
                        style={{
                            fontFamily: CX.font,
                            fontSize: 14,
                            color: CX.textSecondary,
                            letterSpacing: "0.01em",
                            userSelect: "none",
                        }}
                    >
                        {displayHR !== null ? `${displayHR} bpm · ${stateLabel}` : signal.bioStatusMessage}
                    </div>
                ) : (
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
                        <div
                            data-testid="newtab-state-line"
                            style={{ fontSize: 14, color: CX.textSecondary, fontFamily: CX.font, userSelect: "none" }}
                        >
                            Cortex is resting
                        </div>
                        <button
                            className="cortex-launch-button cortex-translucent-material"
                            onClick={handleLaunch}
                            disabled={launching}
                            aria-busy={launching || undefined}
                            style={{
                                padding: "12px 36px",
                                border: "1px solid var(--cx-border-emphasis)",
                                borderRadius: CX.radiusFull,
                                background: GLASS_BACKGROUND,
                                backdropFilter: "blur(20px)",
                                WebkitBackdropFilter: "blur(20px)",
                                color: CX.text,
                                fontSize: 13,
                                fontWeight: 500,
                                fontFamily: CX.font,
                                cursor: launching ? "default" : "pointer",
                                opacity: launching ? 0.6 : 1,
                                boxShadow: CX.shadowFloat,
                                letterSpacing: 0.5,
                            }}
                        >
                            {launching ? "Starting…" : "Open Cortex"}
                        </button>
                        {launchError && (
                            <div role="status" style={{ fontSize: 12, color: CX.textSecondary, fontFamily: CX.font, maxWidth: 300, lineHeight: 1.5 }}>
                                {launchError}
                            </div>
                        )}
                    </div>
                )}
                <p className="cortex-pulse-caption">
                    {displayHR !== null ? "Light follows your latest camera pulse estimate." : "A little space between one thing and the next."}
                </p>
                <div className="cortex-pulse-announcement" role="status" aria-live="polite" aria-atomic="true">
                    {displayConnected ? signal.stale ? "Waiting for fresh signals" : displayHR !== null ? `Pulse estimate available. ${stateLabel}` : signal.bioStatusMessage : "Cortex is resting"}
                </div>
                </div>
            </main>

            {/* Resume cards — translucent material over the pulse field */}
            {showActivities && activities.length > 0 && (
                <section className="cortex-resume-section" aria-labelledby="cortex-resume-title">
                <h2 id="cortex-resume-title">Pick up where you left off</h2>
                <div
                    className="cortex-resume-list"
                    role="list"
                    aria-label="Recent activities"
                    style={{
                        WebkitOverflowScrolling: "touch",
                    }}
                >
                    {activities.slice(0, 3).map((a, index) => {
                        const pct = Math.max(0, Math.min(100, Math.round(a.completion_pct || a.max_completion_pct || 0)));
                        const posLabel = formatActivityPosition(a.position);
                        const resumeUrl = getResumeUrl(a);
                        return (
                            <div
                                key={a.content_id}
                                role="listitem"
                                className="cortex-resume-item"
                                style={{
                                    width: 220,
                                }}
                            >
                                <a
                                    href={resumeUrl}
                                    className="cortex-resume-card cortex-motion-enter cortex-translucent-material"
                                    style={{
                                        display: "block",
                                        textDecoration: "none",
                                        width: "100%",
                                        padding: 16,
                                        borderRadius: 20,
                                        background: GLASS_BACKGROUND,
                                        backdropFilter: "blur(24px)",
                                        WebkitBackdropFilter: "blur(24px)",
                                        boxShadow: `${CX.shadowFloat}, inset 0 0 0 1px var(--cx-border-subtle)`,
                                        cursor: "pointer",
                                        animationDelay: `${index * 50}ms`,
                                    }}
                                    title={a.title}
                                >
                                    <div style={{
                                        fontSize: 14,
                                        fontWeight: 500,
                                        color: CX.text,
                                        whiteSpace: "nowrap",
                                        overflow: "hidden",
                                        textOverflow: "ellipsis",
                                        fontFamily: CX.font,
                                        marginBottom: 10,
                                        letterSpacing: "-0.01em",
                                    }}>{a.title}</div>
                                    {/* Progress bar — 3px */}
                                    <div style={{ height: 3, borderRadius: 1.5, background: "var(--cx-border-emphasis)", marginBottom: 10, overflow: "hidden" }}>
                                        <div style={{ height: "100%", borderRadius: 1.5, background: CX.accent, width: `${pct}%` }} />
                                    </div>
                                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                                        <span style={{ fontSize: 11, color: CX.textTertiary, fontFamily: CX.mono, letterSpacing: "0.05em" }}>{posLabel}</span>
                                        <span style={{ fontSize: 11, fontWeight: 600, color: CX.textSecondary, letterSpacing: "0.08em", textTransform: "uppercase" }}>Resume &rarr;</span>
                                    </div>
                                </a>
                            </div>
                        );
                    })}
                </div>
                </section>
            )}

            {/* Brand watermark — bottom-right, whispered */}
            {!showActivities && <div
                style={{
                    padding: "0 32px 24px",
                    textAlign: "right",
                    fontSize: 11,
                    color: CX.textTertiary,
                    fontFamily: CX.fontSerif,
                    letterSpacing: "0.05em",
                    userSelect: "none",
                    zIndex: 1,
                }}
            >
                Cortex
            </div>}
        </div>
    );
}

function formatActivityPosition(pos: Record<string, unknown>): string {
    switch (pos.type) {
        case "video": {
            const ts = pos.timestamp_s as number;
            const dur = pos.duration_s as number;
            return `${fmtSec(ts)} / ${fmtSec(dur)}`;
        }
        case "scroll":
            return `${Math.round(pos.scroll_pct as number)}% read`;
        case "code_problem":
            return `${pos.stage} · ${pos.wrong_answer_count} WA`;
        case "notebook":
            return `cell ${(pos.cell_index as number) + 1}`;
        case "pdf":
            return `p${pos.page}/${pos.total_pages}`;
        case "slides":
            return `slide ${(pos.slide_index as number) + 1}`;
        default:
            return `${Math.round((pos.scroll_pct as number) || 0)}%`;
    }
}

function fmtSec(s: number): string {
    const total = Math.floor(s);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const sec = total % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    return `${m}:${String(sec).padStart(2, "0")}`;
}

function getResumeUrl(a: { url: string; position: Record<string, unknown> }): string {
    let url = a.url;
    if (a.position.type === "video") {
        const t = Math.floor(a.position.timestamp_s as number);
        if (url.includes("youtube.com") || url.includes("youtu.be")) {
            url += (url.includes("?") ? "&" : "?") + `t=${t}`;
        } else if (url.includes("bilibili.com")) {
            url += (url.includes("?") ? "&" : "?") + `t=${t}`;
        }
    }
    return url;
}

export default PulseRoom;
