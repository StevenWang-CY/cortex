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

const STATE_COLORS: Record<string, { r: number; g: number; b: number }> = {
    FLOW: { r: 16, g: 185, b: 129 },     // emerald #10b981
    HYPER: { r: 239, g: 68, b: 68 },      // red #ef4444
    HYPO: { r: 59, g: 130, b: 246 },      // blue #3b82f6
    RECOVERY: { r: 245, g: 158, b: 11 },  // amber #f59e0b
};

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
    const [displayState, setDisplayState] = useState("");
    const [displayConnected, setDisplayConnected] = useState(false);

    // Activity tracking — "Continue where you left off"
    const [activities, setActivities] = useState<RecentActivity[]>([]);
    const [showActivities, setShowActivities] = useState(false);

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
                        setDisplayState(response.state.state || "");
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

        // Listen for live updates
        const listener = (message: Record<string, unknown>) => {
            if (message.type === "STATE_UPDATE" && message.payload) {
                const payload = message.payload as Record<string, unknown>;
                stateRef.current.state = (payload.state as string) || "";
                stateRef.current.confidence = (payload.confidence as number) || 0;
                stateRef.current.connected = true;
                setDisplayState((payload.state as string) || "");
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

    // Fetch recent activities with delay (keeps Pulse Room's ghostly feel)
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
            const col = STATE_COLORS[sr.state] || STATE_COLORS.FLOW;

            // Fade trail
            ctx.fillStyle = "rgba(9, 9, 11, 0.15)";
            ctx.fillRect(0, 0, w, h);

            // Beat timing
            if (sr.heartRate > 0) {
                const timeSinceBeat = now - anim.lastBeatTime;
                anim.pulsePhase = Math.min(timeSinceBeat / anim.beatInterval, 1);

                if (timeSinceBeat >= anim.beatInterval) {
                    anim.lastBeatTime = now;
                    anim.pulsePhase = 0;
                    anim.rings.push({
                        born: now,
                        radius: 0,
                        maxRadius: 150 + Math.random() * 80,
                        opacity: 0.3,
                    });
                }
            }

            // Background breathing
            anim.breathPhase += 0.002;
            const breathVal = Math.sin(anim.breathPhase) * 0.5 + 0.5;
            const bgGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(w, h) * 0.7);
            bgGrad.addColorStop(0, `rgba(${col.r}, ${col.g}, ${col.b}, ${0.02 + breathVal * 0.01})`);
            bgGrad.addColorStop(1, "rgba(9, 9, 11, 0)");
            ctx.fillStyle = bgGrad;
            ctx.fillRect(0, 0, w, h);

            // Ripple rings
            anim.rings = anim.rings.filter((ring) => {
                const age = (now - ring.born) / 1000;
                if (age > 4) return false;

                ring.radius = ring.maxRadius * (1 - Math.exp(-age * 1.5));
                const fadeAlpha = Math.max(0, ring.opacity * (1 - age / 4));

                ctx.beginPath();
                ctx.arc(cx, cy, ring.radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, ${fadeAlpha})`;
                ctx.lineWidth = 1.5 * (1 - age / 4);
                ctx.stroke();
                return true;
            });

            // Central pulse orb
            if (sr.heartRate > 0) {
                const systole =
                    anim.pulsePhase < 0.15
                        ? Math.sin((anim.pulsePhase / 0.15) * (Math.PI / 2))
                        : Math.exp(-(anim.pulsePhase - 0.15) * 4);

                const orbRadius = 20 + systole * 25;
                const orbOpacity = 0.3 + systole * 0.5;

                // Outer glow
                const glowGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, orbRadius * 4);
                glowGrad.addColorStop(0, `rgba(${col.r}, ${col.g}, ${col.b}, ${orbOpacity * 0.15})`);
                glowGrad.addColorStop(0.5, `rgba(${col.r}, ${col.g}, ${col.b}, ${orbOpacity * 0.05})`);
                glowGrad.addColorStop(1, `rgba(${col.r}, ${col.g}, ${col.b}, 0)`);
                ctx.fillStyle = glowGrad;
                ctx.beginPath();
                ctx.arc(cx, cy, orbRadius * 4, 0, Math.PI * 2);
                ctx.fill();

                // Core
                const coreGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, orbRadius);
                coreGrad.addColorStop(0, `rgba(255, 255, 255, ${orbOpacity * 0.9})`);
                coreGrad.addColorStop(0.3, `rgba(${col.r}, ${col.g}, ${col.b}, ${orbOpacity * 0.7})`);
                coreGrad.addColorStop(1, `rgba(${col.r}, ${col.g}, ${col.b}, 0)`);
                ctx.fillStyle = coreGrad;
                ctx.beginPath();
                ctx.arc(cx, cy, orbRadius, 0, Math.PI * 2);
                ctx.fill();

                // ECG trace
                anim.traceHistory.push(systole);
                if (anim.traceHistory.length > 200) anim.traceHistory.shift();

                const traceY = h - 100;
                const traceW = Math.min(w * 0.6, 600);
                const traceX = (w - traceW) / 2;

                ctx.beginPath();
                ctx.strokeStyle = `rgba(${col.r}, ${col.g}, ${col.b}, 0.12)`;
                ctx.lineWidth = 1.5;
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
                    ctx.fillStyle = `rgba(${col.r}, ${col.g}, ${col.b}, 0.4)`;
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
    }, []);

    const col = STATE_COLORS[displayState] || STATE_COLORS.FLOW;

    return (
        <div
            style={{
                width: "100vw",
                height: "100vh",
                overflow: "hidden",
                background: "#09090b",
                margin: 0,
                padding: 0,
            }}
        >
            <canvas
                ref={canvasRef}
                style={{ display: "block", width: "100%", height: "100%" }}
            />
            <div
                style={{
                    position: "fixed",
                    bottom: 56,
                    left: "50%",
                    transform: "translateX(-50%)",
                    fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', ui-monospace, monospace",
                    fontSize: 36,
                    fontWeight: 200,
                    letterSpacing: -1,
                    color: "rgba(228, 228, 231, 0.06)",
                    userSelect: "none",
                    transition: "color 3s ease",
                }}
            >
                {displayHR > 0 ? displayHR : "--"}
                <span style={{ fontSize: 11, letterSpacing: 1, color: "rgba(228,228,231,0.04)", marginLeft: 4 }}>
                    bpm
                </span>
            </div>
            <div
                style={{
                    position: "fixed",
                    bottom: 34,
                    left: "50%",
                    transform: "translateX(-50%)",
                    fontFamily: "'SF Mono', 'Fira Code', ui-monospace, monospace",
                    fontSize: 10,
                    letterSpacing: 1.5,
                    color: displayConnected
                        ? `rgba(${col.r}, ${col.g}, ${col.b}, 0.15)`
                        : "rgba(228, 228, 231, 0.06)",
                    userSelect: "none",
                    transition: "color 3s ease",
                }}
            >
                {displayConnected ? displayState.toLowerCase() || "connecting" : "connecting"}
            </div>
            {/* Continue where you left off */}
            {showActivities && activities.length > 0 && (
                <div
                    style={{
                        position: "fixed",
                        bottom: 80,
                        left: "50%",
                        transform: "translateX(-50%)",
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "center",
                        gap: 6,
                        opacity: 0,
                        animation: "activityFadeIn 2s ease forwards",
                    }}
                >
                    <style>{`
                        @keyframes activityFadeIn {
                            from { opacity: 0; transform: translateY(8px); }
                            to { opacity: 1; transform: translateY(0); }
                        }
                    `}</style>
                    {activities.map((a) => {
                        const posLabel = formatActivityPosition(a.position);
                        const resumeUrl = getResumeUrl(a);
                        return (
                            <a
                                key={a.content_id}
                                href={resumeUrl}
                                style={{
                                    display: "block",
                                    textDecoration: "none",
                                    color: "rgba(228, 228, 231, 0.08)",
                                    fontFamily: "'SF Mono', 'Fira Code', ui-monospace, monospace",
                                    fontSize: 10,
                                    letterSpacing: 0.5,
                                    padding: "3px 8px",
                                    borderRadius: 4,
                                    transition: "color 0.5s ease, background 0.3s ease",
                                    background: "transparent",
                                    maxWidth: 400,
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                    whiteSpace: "nowrap" as const,
                                    cursor: "pointer",
                                }}
                                onMouseEnter={(e) => {
                                    e.currentTarget.style.color = "rgba(228, 228, 231, 0.25)";
                                    e.currentTarget.style.background = "rgba(255,255,255,0.02)";
                                }}
                                onMouseLeave={(e) => {
                                    e.currentTarget.style.color = "rgba(228, 228, 231, 0.08)";
                                    e.currentTarget.style.background = "transparent";
                                }}
                                title={a.title}
                            >
                                {a.title.slice(0, 40)}{a.title.length > 40 ? "..." : ""} — {posLabel}
                            </a>
                        );
                    })}
                </div>
            )}
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
