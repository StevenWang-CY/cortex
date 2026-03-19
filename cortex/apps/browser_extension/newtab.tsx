/**
 * Cortex Pulse Room — New Tab Page
 *
 * Renders your heartbeat as light. A single point of light in darkness
 * that pulses at your actual heart rate. Pulse history ripples outward.
 * ECG-style trace at the bottom.
 *
 * Inspired by Rafael Lozano-Hemmer's Pulse Room installation.
 */

import React, { useEffect, useRef, useState } from "react";
import "./page-reset.css";
import { CX, STATE_COLORS_RGB, CX_KEYFRAMES } from "./design-tokens";

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
    });

    const [displayHR, setDisplayHR] = useState(0);
    const [displayConnected, setDisplayConnected] = useState(false);
    const [reducedMotion, setReducedMotion] = useState(false);

    // Launch controls
    const [launching, setLaunching] = useState(false);
    const [launchError, setLaunchError] = useState("");

    // Activity tracking — resume cards at bottom
    const [activities, setActivities] = useState<RecentActivity[]>([]);
    const [showActivities, setShowActivities] = useState(false);

    // Inject fonts + keyframes
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
        `;
        document.head.appendChild(style);

        const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
        setReducedMotion(mq.matches);
        const handler = (e: MediaQueryListEvent) => setReducedMotion(e.matches);
        mq.addEventListener("change", handler);

        return () => {
            document.head.removeChild(style);
            mq.removeEventListener("change", handler);
        };
    }, []);

    // Poll background for state
    useEffect(() => {
        function poll() {
            try {
                chrome.runtime.sendMessage({ type: "GET_STATE" }, (response) => {
                    if (chrome.runtime.lastError || !response) return;
                    stateRef.current.connected = response.connected;
                    setDisplayConnected(response.connected);
                    if (response.state) {
                        stateRef.current.state = response.state.state || "";
                        stateRef.current.confidence = response.state.confidence || 0;
                        const bio = response.state.biometrics;
                        if (bio?.heart_rate) {
                            stateRef.current.heartRate = Math.round(bio.heart_rate);
                            animRef.current.beatInterval = 60000 / bio.heart_rate;
                            setDisplayHR(Math.round(bio.heart_rate));
                        }
                    }
                });
            } catch {
                /* extension context lost */
            }
        }

        poll();
        const interval = setInterval(poll, 3000);

        const listener = (message: Record<string, unknown>) => {
            if (message.type === "STATE_UPDATE" && message.payload) {
                const payload = message.payload as Record<string, unknown>;
                stateRef.current.state = (payload.state as string) || "";
                stateRef.current.confidence = (payload.confidence as number) || 0;
                stateRef.current.connected = true;
                setDisplayConnected(true);
                const bio = payload.biometrics as Record<string, number> | undefined;
                if (bio?.heart_rate) {
                    stateRef.current.heartRate = Math.round(bio.heart_rate);
                    animRef.current.beatInterval = 60000 / bio.heart_rate;
                    setDisplayHR(Math.round(bio.heart_rate));
                }
            }
        };
        chrome.runtime.onMessage.addListener(listener);

        return () => {
            clearInterval(interval);
            chrome.runtime.onMessage.removeListener(listener);
        };
    }, []);

    // Fetch recent activities with delay
    useEffect(() => {
        const timer = setTimeout(() => {
            try {
                chrome.runtime.sendMessage({ type: "GET_RECENT_ACTIVITIES", limit: 3 }, (result) => {
                    if (chrome.runtime.lastError || !Array.isArray(result)) return;
                    setActivities(result);
                    if (result.length > 0) {
                        setTimeout(() => setShowActivities(true), 500);
                    }
                });
            } catch { /* context lost */ }
        }, 2000);
        return () => clearTimeout(timer);
    }, []);

    // Canvas animation loop
    useEffect(() => {
        if (reducedMotion) return;

        const canvas = canvasRef.current;
        if (!canvas) return;

        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        let rafId: number;

        function resize() {
            if (!canvas) return;
            const dpr = window.devicePixelRatio || 1;
            canvas.width = window.innerWidth * dpr;
            canvas.height = window.innerHeight * dpr;
            ctx!.scale(dpr, dpr);
            // Fill immediately on resize to prevent white flash
            ctx!.fillStyle = CX.bg;
            ctx!.fillRect(0, 0, window.innerWidth, window.innerHeight);
        }
        resize();
        window.addEventListener("resize", resize);

        function draw(now: number) {
            rafId = requestAnimationFrame(draw);
            if (!ctx || !canvas) return;

            const w = window.innerWidth;
            const h = window.innerHeight;
            const cx = w / 2;
            const cy = h / 2 - 40;

            const sr = stateRef.current;
            const anim = animRef.current;
            const col = STATE_COLORS_RGB[sr.state] || STATE_COLORS_RGB.FLOW;
            const isHyper = sr.state === "HYPER";

            // Fade trail — use warm bg color
            ctx.fillStyle = "rgba(12, 12, 14, 0.15)";
            ctx.fillRect(0, 0, w, h);

            // Beat timing
            if (sr.heartRate > 0) {
                const timeSinceBeat = now - anim.lastBeatTime;
                anim.pulsePhase = Math.min(timeSinceBeat / anim.beatInterval, 1);

                if (timeSinceBeat >= anim.beatInterval) {
                    anim.lastBeatTime = now;
                    anim.pulsePhase = 0;
                    if (!isHyper || anim.rings.length % 2 === 0) {
                        anim.rings.push({
                            born: now,
                            radius: 0,
                            maxRadius: 150 + Math.random() * 80,
                            opacity: isHyper ? 0.15 : 0.3,
                        });
                    }
                }
            }

            // Background breathing (disabled during HYPER)
            if (!isHyper) {
                anim.breathPhase += 0.002;
            }
            const breathVal = Math.sin(anim.breathPhase) * 0.5 + 0.5;
            const bgGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(w, h) * 0.7);
            bgGrad.addColorStop(0, `rgba(${col.r}, ${col.g}, ${col.b}, ${0.02 + breathVal * 0.01})`);
            bgGrad.addColorStop(1, "rgba(12, 12, 14, 0)");
            ctx.fillStyle = bgGrad;
            ctx.fillRect(0, 0, w, h);

            // Ripple rings — state color at 3% opacity, fade over 3 seconds
            anim.rings = anim.rings.filter((ring) => {
                const age = (now - ring.born) / 1000;
                if (age > 3) return false;

                ring.radius = ring.maxRadius * (1 - Math.exp(-age * 1.5));
                const fadeAlpha = Math.max(0, 0.03 * (1 - age / 3));

                ctx.beginPath();
                ctx.arc(cx, cy, ring.radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, ${fadeAlpha})`;
                ctx.lineWidth = 1.5 * (1 - age / 3);
                ctx.stroke();
                return true;
            });

            // Central pulse orb — 8-12% opacity glow
            if (sr.heartRate > 0) {
                const systole =
                    anim.pulsePhase < 0.15
                        ? Math.sin((anim.pulsePhase / 0.15) * (Math.PI / 2))
                        : Math.exp(-(anim.pulsePhase - 0.15) * 4);

                const dampened = isHyper ? systole * 0.5 : systole;
                const orbRadius = 20 + dampened * 25;
                const orbOpacity = 0.08 + dampened * 0.04; // 8-12% per guide

                // Outer glow
                const glowRadius = isHyper ? orbRadius * 2.5 : orbRadius * 4;
                const glowGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, glowRadius);
                glowGrad.addColorStop(0, `rgba(${col.r}, ${col.g}, ${col.b}, ${orbOpacity})`);
                glowGrad.addColorStop(0.5, `rgba(${col.r}, ${col.g}, ${col.b}, ${orbOpacity * 0.4})`);
                glowGrad.addColorStop(1, `rgba(${col.r}, ${col.g}, ${col.b}, 0)`);
                ctx.fillStyle = glowGrad;
                ctx.beginPath();
                ctx.arc(cx, cy, glowRadius, 0, Math.PI * 2);
                ctx.fill();

                // Core
                const coreGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, orbRadius);
                coreGrad.addColorStop(0, `rgba(255, 255, 255, ${orbOpacity * 3})`);
                coreGrad.addColorStop(0.3, `rgba(${col.r}, ${col.g}, ${col.b}, ${orbOpacity * 2})`);
                coreGrad.addColorStop(1, `rgba(${col.r}, ${col.g}, ${col.b}, 0)`);
                ctx.fillStyle = coreGrad;
                ctx.beginPath();
                ctx.arc(cx, cy, orbRadius, 0, Math.PI * 2);
                ctx.fill();

                // ECG trace — 1px, state color at 30% opacity
                anim.traceHistory.push(systole);
                if (anim.traceHistory.length > 200) anim.traceHistory.shift();

                const traceY = cy + 70;
                const traceW = Math.min(w * 0.6, 600);
                const traceX = (w - traceW) / 2;

                ctx.beginPath();
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, 0.3)`;
                ctx.lineWidth = 1;
                for (let i = 0; i < anim.traceHistory.length; i++) {
                    const x = traceX + (i / 200) * traceW;
                    const y = traceY - anim.traceHistory[i] * 30;
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                }
                ctx.stroke();

                // Scanning dot
                if (anim.traceHistory.length > 0) {
                    const dotX = traceX + ((anim.traceHistory.length - 1) / 200) * traceW;
                    const dotY = traceY - anim.traceHistory[anim.traceHistory.length - 1] * 30;
                    ctx.beginPath();
                    ctx.arc(dotX, dotY, 3, 0, Math.PI * 2);
                    ctx.fillStyle = `rgba(${col.r}, ${col.g}, ${col.b}, 0.3)`;
                    ctx.fill();
                }
            } else {
                // Idle breathing
                const idleRadius = 15 + Math.sin(now / 2000) * 5;
                const idleGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, idleRadius * 3);
                idleGrad.addColorStop(0, "rgba(113, 113, 122, 0.12)");
                idleGrad.addColorStop(1, "rgba(113, 113, 122, 0)");
                ctx.fillStyle = idleGrad;
                ctx.beginPath();
                ctx.arc(cx, cy, idleRadius * 3, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        rafId = requestAnimationFrame(draw);

        return () => {
            cancelAnimationFrame(rafId);
            window.removeEventListener("resize", resize);
        };
    }, [reducedMotion]);

    function handleLaunch() {
        setLaunching(true);
        setLaunchError("");
        chrome.runtime.sendMessage({ type: "LAUNCH_CORTEX" }, (resp) => {
            setLaunching(false);
            if (resp?.ok && resp.status === "camera_enabled") {
                // Connected — state updates will flow via polling
            } else {
                setLaunchError(`${resp?.status || "no_response"}: ${resp?.error || "unknown"}`);
                setTimeout(() => setLaunchError(""), 15000);
            }
        });
    }

    const col = STATE_COLORS_RGB[displayConnected ? stateRef.current.state : ""] || STATE_COLORS_RGB.FLOW;

    return (
        <div
            style={{
                width: "100vw",
                height: "100vh",
                overflow: "hidden",
                background: CX.bg,
                margin: 0,
                padding: 0,
            }}
        >
            <canvas
                ref={canvasRef}
                style={{ display: "block", width: "100%", height: "100%" }}
            />

            {/* Static orb for reduced motion */}
            {reducedMotion && (
                <div style={{
                    position: "fixed",
                    top: "50%",
                    left: "50%",
                    transform: "translate(-50%, -50%)",
                    width: 40,
                    height: 40,
                    borderRadius: "50%",
                    background: `radial-gradient(circle, rgba(${col.r},${col.g},${col.b},0.1), rgba(${col.r},${col.g},${col.b},0.02))`,
                }} />
            )}

            {/* Start button — shown when daemon is not connected */}
            {!displayConnected && !displayHR && (
                <div style={{
                    position: "fixed",
                    top: "calc(50% + 60px)",
                    left: "50%",
                    transform: "translateX(-50%)",
                    display: "flex",
                    flexDirection: "column" as const,
                    alignItems: "center",
                    gap: 10,
                }}>
                    <button
                        onClick={handleLaunch}
                        disabled={launching}
                        style={{
                            padding: "10px 32px",
                            border: `1px solid ${CX.border}`,
                            borderRadius: CX.radiusLg,
                            background: launching ? CX.surface : "transparent",
                            color: CX.textSecondary,
                            fontSize: 13,
                            fontWeight: 500,
                            fontFamily: CX.font,
                            cursor: launching ? "default" : "pointer",
                            opacity: launching ? 0.6 : 1,
                            transition: `all ${CX.durationNormal} ${CX.easeDefault}`,
                            letterSpacing: 0.3,
                        }}
                    >
                        {launching ? "Starting\u2026" : "Start Cortex"}
                    </button>
                    {launchError && (
                        <div style={{
                            fontSize: 10,
                            color: CX.textTertiary,
                            fontFamily: CX.mono,
                            textAlign: "center" as const,
                            maxWidth: 300,
                            lineHeight: 1.5,
                        }}>
                            {launchError}
                        </div>
                    )}
                </div>
            )}

            {/* BPM readout — 36px, mono, centered below orb (48px gap). No label. Just the number. Hidden when no data. */}
            {displayHR > 0 && (
                <div
                    style={{
                        position: "fixed",
                        top: "calc(50% + 60px)",
                        left: "50%",
                        transform: "translateX(-50%)",
                        fontFamily: CX.mono,
                        fontSize: 36,
                        fontWeight: 600,
                        color: CX.text,
                        userSelect: "none",
                        transition: "color 3s ease, opacity 1s ease",
                        lineHeight: 1.15,
                    }}
                    aria-label={`${displayHR} beats per minute`}
                >
                    {displayHR}
                </div>
            )}

            {/* Resume cards — bottom, horizontal, max 3, each max 200px */}
            {showActivities && activities.length > 0 && (
                <div
                    style={{
                        position: "fixed",
                        bottom: 24,
                        left: 24,
                        display: "flex",
                        gap: 12,
                        opacity: 0,
                        animation: "activityFadeIn 2s ease forwards",
                    }}
                >
                    {activities.slice(0, 3).map((a) => {
                        const pct = Math.round(a.completion_pct || a.max_completion_pct || 0);
                        const posLabel = formatActivityPosition(a.position);
                        const resumeUrl = getResumeUrl(a);
                        return (
                            <a
                                key={a.content_id}
                                href={resumeUrl}
                                style={{
                                    display: "block",
                                    textDecoration: "none",
                                    maxWidth: 200,
                                    padding: 12,
                                    borderRadius: CX.radiusLg,
                                    background: CX.surface,
                                    cursor: "pointer",
                                    transition: `background ${CX.durationMicro} ${CX.easeDefault}`,
                                }}
                                onMouseEnter={(e) => { e.currentTarget.style.background = CX.tertiary; }}
                                onMouseLeave={(e) => { e.currentTarget.style.background = CX.surface; }}
                                title={a.title}
                            >
                                <div style={{
                                    fontSize: 13,
                                    color: CX.text,
                                    whiteSpace: "nowrap" as const,
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                    fontFamily: CX.font,
                                    marginBottom: 8,
                                }}>{a.title}</div>
                                {/* Progress bar — 4px, accent fill */}
                                <div style={{ height: 4, borderRadius: 2, background: CX.tertiary, marginBottom: 6, overflow: "hidden" }}>
                                    <div style={{ height: "100%", borderRadius: 2, background: CX.accent, width: `${pct}%` }} />
                                </div>
                                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                                    <span style={{ fontSize: 10, color: CX.textTertiary, fontFamily: CX.mono }}>{posLabel}</span>
                                    <span style={{ fontSize: 11, fontWeight: 500, color: CX.accent, letterSpacing: "0.04em", textTransform: "uppercase" as const }}>Resume</span>
                                </div>
                            </a>
                        );
                    })}
                </div>
            )}

            {/* Brand watermark — bottom-right, whispered */}
            <div
                style={{
                    position: "fixed",
                    bottom: 16,
                    right: 16,
                    fontSize: 10,
                    color: CX.textTertiary,
                    opacity: 0.5,
                    fontFamily: CX.font,
                    userSelect: "none",
                }}
            >
                Cortex
            </div>
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
            return `${pos.stage} \u00b7 ${pos.wrong_answer_count} WA`;
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
