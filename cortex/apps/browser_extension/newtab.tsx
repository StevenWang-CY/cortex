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

    // Canvas animation loop + Logo Interaction
    const logoRef = useRef<HTMLDivElement>(null);
    const auraRef = useRef<HTMLDivElement>(null);

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
            const cy = h / 2 - 80; // Offset upwards to match the SVG layout

            const sr = stateRef.current;
            const anim = animRef.current;
            const col = STATE_COLORS_RGB[sr.state] || STATE_COLORS_RGB.FLOW;
            const isHyper = sr.state === "HYPER";

            // --- Background Paint ---
            ctx.clearRect(0, 0, w, h);
            ctx.fillStyle = CX.bg;
            ctx.fillRect(0, 0, w, h);

            // Beat timing computing based on heartbeat
            if (sr.heartRate > 0) {
                const timeSinceBeat = now - anim.lastBeatTime;
                anim.pulsePhase = Math.min(timeSinceBeat / anim.beatInterval, 1);

                if (timeSinceBeat >= anim.beatInterval) {
                    anim.lastBeatTime = now;
                    anim.pulsePhase = 0;
                    // Spawn delicate ripple ring
                    if (!isHyper || anim.rings.length % 2 === 0) {
                        anim.rings.push({
                            born: now,
                            radius: 30, // Start tightly behind logo
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
                // Extremely faint, delicate lines
                const fadeAlpha = Math.max(0, 0.4 * (1 - age / 4));

                ctx.beginPath();
                ctx.arc(cx, cy, ring.radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, ${fadeAlpha})`;
                ctx.lineWidth = 1;
                ctx.stroke();
                // Add second faint ring for detail
                ctx.beginPath();
                ctx.arc(cx, cy, ring.radius * 0.8, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, ${fadeAlpha * 0.3})`;
                ctx.lineWidth = 0.5;
                ctx.stroke();
                
                return true;
            });

            // --- Drive the SVG DOM Elements ---
            const systole = sr.heartRate > 0
                ? (anim.pulsePhase < 0.15
                    ? Math.sin((anim.pulsePhase / 0.15) * (Math.PI / 2))
                    : Math.exp(-(anim.pulsePhase - 0.15) * 5))
                : anim.pulsePhase;

            const dampened = isHyper ? systole * 0.6 : systole;

            if (logoRef.current) {
                // Core heartbeat scaling — delicate and elegant
                const scale = 1 + dampened * 0.08;
                logoRef.current.style.transform = `scale(${scale})`;
            }

            if (auraRef.current) {
                // Glowing drop-shadow "breath" around the logo
                const glowSize = 20 + dampened * 60;
                const glowAlpha = 0.05 + dampened * 0.15;
                auraRef.current.style.filter = `drop-shadow(0px 0px ${glowSize}px rgba(${col.r}, ${col.g}, ${col.b}, ${glowAlpha}))`;
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
                    position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                    background: `radial-gradient(circle at 50% 50%, rgba(${col.r},${col.g},${col.b},0.08), ${CX.bg})`,
                    zIndex: 0,
                }} />
            )}

            {/* Premium Editorial Centerpiece & Breathing SVG Logo */}
            <div
                style={{
                    position: "absolute",
                    top: "50%",
                    left: "50%",
                    transform: "translate(-50%, -50%)",
                    zIndex: 10,
                }}
            >
                <div style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                    textAlign: "center",
                    animation: "activityFadeIn 2.5s cubic-bezier(0.16, 1, 0.3, 1) forwards",
                }}>
                {/* The Breathing Logo */}
                <div 
                    ref={auraRef}
                    style={{
                        marginBottom: 40,
                        transition: "filter 0.05s linear", 
                    }}
                >
                    <div 
                        ref={logoRef}
                        style={{
                            color: displayConnected ? `rgb(${col.r}, ${col.g}, ${col.b})` : CX.textSecondary,
                            willChange: "transform",
                            transformOrigin: "center center",
                            transition: "transform 0.05s linear",
                        }}
                    >
                        <svg width="100" height="100" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
                            <path d="M 51.8 12.2 A 28 28 0 1 0 51.8 51.8" fill="none" stroke="#1a1a1a" strokeWidth="6" strokeLinecap="round" />
                            <path d="M 12 32 L 22 32 L 27 15 L 37 49 L 42 32 L 60 32" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
                            <circle cx="60" cy="32" r="3" fill="currentColor" />
                        </svg>
                    </div>
                </div>

                <h1 style={{
                    fontFamily: CX.fontSerif,
                    fontSize: 48,
                    fontWeight: 400,
                    color: CX.text,
                    letterSpacing: "-0.02em",
                    margin: "0 0 16px 0",
                    userSelect: "none",
                }}>
                    {displayConnected ? (
                        <>
                            {stateRef.current.state === "FLOW" ? "Deep Work with " :
                             stateRef.current.state === "HYPER" ? "Elevated with " :
                             stateRef.current.state === "RECOVERY" ? "Recovery with " : "Resting with "}
                            <span style={{ fontFamily: CX.fontBrand, fontStyle: "italic", fontWeight: 500, paddingLeft: 4, letterSpacing: "0.02em" }}>Cortex.</span>
                        </>
                    ) : (
                        <span style={{ fontFamily: CX.fontBrand, fontStyle: "italic", fontWeight: 500, letterSpacing: "0.02em" }}>Cortex.</span>
                    )}
                </h1>
                
                {displayConnected ? (
                    <div style={{
                        fontFamily: CX.mono,
                        fontSize: 13,
                        color: CX.textTertiary,
                        textTransform: "uppercase",
                        letterSpacing: "0.15em",
                        userSelect: "none",
                    }}>
                        {displayHR > 0 ? `${displayHR} BPM \u00b7 ${stateRef.current.state} STATE` : "CALIBRATING BIOFEEDBACK"}
                    </div>
                ) : (
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
                        <div style={{ fontSize: 13, color: CX.textTertiary, fontFamily: CX.mono, letterSpacing: "0.15em", userSelect: "none" }}>
                            VISUAL ENGINE OFFLINE
                        </div>
                        <button
                            onClick={handleLaunch}
                            disabled={launching}
                            style={{
                                padding: "12px 36px",
                                border: `1px solid rgba(0,0,0,0.08)`,
                                borderRadius: CX.radiusFull,
                                background: "rgba(255, 255, 255, 0.6)",
                                backdropFilter: "blur(20px)",
                                WebkitBackdropFilter: "blur(20px)",
                                color: CX.text,
                                fontSize: 13,
                                fontWeight: 500,
                                fontFamily: CX.font,
                                cursor: launching ? "default" : "pointer",
                                opacity: launching ? 0.6 : 1,
                                boxShadow: "0 4px 12px rgba(0,0,0,0.03)",
                                transition: `all ${CX.durationNormal} ${CX.easeDefault}`,
                                letterSpacing: 0.5,
                            }}
                        >
                            {launching ? "Starting\u2026" : "Start Cortex"}
                        </button>
                        {launchError && (
                            <div style={{ fontSize: 11, color: CX.textTertiary, fontFamily: CX.mono, maxWidth: 300, lineHeight: 1.5 }}>
                                {launchError}
                            </div>
                        )}
                    </div>
                )}
                </div>
            </div>

            {/* Resume cards — Glassmorphic Artifacts */}
            {showActivities && activities.length > 0 && (
                <div
                    style={{
                        position: "absolute",
                        bottom: 32,
                        left: 32,
                        display: "flex",
                        gap: 16,
                        zIndex: 10,
                        opacity: 0,
                        animation: "activityFadeIn 2s cubic-bezier(0.16, 1, 0.3, 1) 0.5s forwards",
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
                                    width: 220,
                                    padding: 16,
                                    borderRadius: 20,
                                    background: "rgba(255, 255, 255, 0.65)",
                                    backdropFilter: "blur(24px)",
                                    WebkitBackdropFilter: "blur(24px)",
                                    boxShadow: "0 8px 32px rgba(0, 0, 0, 0.05), inset 0 0 0 1px rgba(255,255,255,0.6)",
                                    cursor: "pointer",
                                    transition: `transform ${CX.durationNormal} ${CX.easeDefault}, box-shadow ${CX.durationNormal} ${CX.easeDefault}`,
                                }}
                                onMouseEnter={(e) => { 
                                    e.currentTarget.style.transform = "translateY(-4px)";
                                    e.currentTarget.style.boxShadow = "0 12px 40px rgba(0, 0, 0, 0.08), inset 0 0 0 1px rgba(255,255,255,0.8)";
                                }}
                                onMouseLeave={(e) => { 
                                    e.currentTarget.style.transform = "translateY(0)";
                                    e.currentTarget.style.boxShadow = "0 8px 32px rgba(0, 0, 0, 0.05), inset 0 0 0 1px rgba(255,255,255,0.6)";
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
                                {/* Progress bar — 3px, sleek */}
                                <div style={{ height: 3, borderRadius: 1.5, background: "rgba(0,0,0,0.06)", marginBottom: 10, overflow: "hidden" }}>
                                    <div style={{ height: "100%", borderRadius: 1.5, background: CX.accent, width: `${pct}%` }} />
                                </div>
                                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                                    <span style={{ fontSize: 10, color: CX.textTertiary, fontFamily: CX.mono, letterSpacing: "0.05em" }}>{posLabel}</span>
                                    <span style={{ fontSize: 10, fontWeight: 600, color: CX.textSecondary, letterSpacing: "0.08em", textTransform: "uppercase" }}>Resume &rarr;</span>
                                </div>
                            </a>
                        );
                    })}
                </div>
            )}

            {/* Brand watermark — bottom-right, whispered */}
            <div
                style={{
                    position: "absolute",
                    bottom: 24,
                    right: 32,
                    fontSize: 11,
                    color: CX.textTertiary,
                    opacity: 0.4,
                    fontFamily: CX.fontSerif,
                    letterSpacing: "0.05em",
                    userSelect: "none",
                    zIndex: 1,
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
