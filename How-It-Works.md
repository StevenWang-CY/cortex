# How It Works

Cortex is a five-layer real-time pipeline. It captures biological and behavioral signals, classifies your cognitive state every 500ms, and when you're overwhelmed it uses an LLM to generate specific workspace interventions.

---

## Signal Pipeline

```
Webcam (30 FPS)
     │
     ▼
L1: Bio-Extraction ─── rPPG · Blink · Pose · Telemetry
     │
     ▼  FeatureVector (500ms)
L2: State Engine ────── Fusion · Scoring · Smoothing · Detectors
     │
     ▼  StateEstimate
L3: Context Engine ──── VS Code · Chrome · Terminal
     │
     ▼  TaskContext
L4: LLM Engine ──────── Azure OpenAI · Ollama · Bandit
     │
     ▼  InterventionPlan
L5: Intervention ────── Consent · Execute · Undo · Learn
```

---

## L1: Bio-Extraction

Cortex reads your biology from two sources simultaneously.

### Remote Photoplethysmography (rPPG)

Your heart pumping blood causes tiny color changes in your skin — imperceptible to the eye but visible to a camera. Cortex extracts RGB traces from your forehead and cheeks at 30 FPS, applies a bandpass filter (0.7–3.5 Hz), runs the POS or CHROM algorithm to separate the pulse signal from motion noise, and estimates:

- **Heart rate** (BPM)
- **Heart rate variability** (RMSSD — a proxy for stress and recovery)
- **5-second HR delta** (rate of change)

Signal quality degrades in low light, with large head movements, or if the face is partially occluded. When quality drops, the system falls back to telemetry-only detection with stricter confidence thresholds.

### Kinematics

- **Blink rate** — blink suppression (rate < 50% of baseline) indicates hyperfocus or stress
- **Head pose** — pitch/yaw/roll; freeze and jitter detection
- **Posture** — shoulder drop, forward lean, slump score via MediaPipe full-body pose

### Behavioral Telemetry

Mouse and keyboard patterns captured via `pynput` at 60 Hz, downsampled to 10 Hz:

- Mouse velocity mean and variance
- Mouse jerk (second derivative of velocity)
- Click burst score
- Keyboard burst score, keystroke interval variance, backspace density
- Window switch rate

---

## L2: State Classification

### Feature Fusion

Every 500ms, PhysioFeatures + KinematicFeatures + TelemetryFeatures are merged into a 12-dimensional normalized FeatureVector. Missing channels are handled via confidence weighting — if the webcam signal is poor, physio features are down-weighted, not dropped.

### Rule Scorer

Each sub-scorer compares the feature against your personal baseline (from calibration) and returns a 0–1 score:

| Sub-scorer | Weight | What it detects |
|------------|--------|-----------------|
| Pulse elevation | 0.20 | HR > 15 BPM above resting |
| HRV drop | 0.15 | RMSSD > 40% below resting |
| Blink suppression | 0.12 | Blink rate < 50% of baseline |
| Posture collapse | 0.08 | Slump + forward lean + shoulder drop |
| Mouse thrashing | 0.15 | Velocity variance > 3× baseline |
| Window switching | 0.15 | Rapid tab/window changes |
| Workspace complexity | 0.15 | Error count + tab count + context load |

### Smoothing and Hysteresis

Raw scores pass through:
1. **EMA smoother** (alpha = 0.3) — prevents single-frame spikes from triggering
2. **Hysteresis** (entry = 0.85, exit = 0.70) — requires sustained signal to enter and exit states
3. **Dwell enforcement** (HYPER: 8s minimum, HYPO: 15s) — prevents rapid oscillation

### Cognitive States

| State | Description |
|-------|-------------|
| **FLOW** | Focused, productive work. No intervention. |
| **HYPER** | Overwhelmed, thrashing, stuck. Primary intervention target. |
| **HYPO** | Disengaged, zombie-scrolling, drifting. Softer prompts. |
| **RECOVERY** | Transitioning back to focus after HYPER/HYPO. |

### v2.0 Specialized Detectors

| Detector | Purpose |
|----------|---------|
| `stress_integral` | Cumulative HRV suppression — replaces Pomodoro with biology-driven breaks |
| `zombie_detector` | HYPO + browser + low mouse + high blink for 90+ seconds |
| `rabbit_hole` | Goal drift when alignment < 30% after 10+ minutes |
| `amygdala_hijack` | Acute stress spike (LeetCode mode) |
| `destructive_struggle` | Productive → destructive transition (LeetCode mode) |
| `parasympathetic_rebound` | Optimal learning window post-accept (LeetCode mode) |

---

## L3: Context Engine

When an intervention is warranted, Cortex gathers workspace context:

- **VS Code adapter** — open file path, visible code range, cursor symbol, active diagnostics (errors/warnings)
- **Chrome adapter** — active tab title/URL, all open tabs (sampled to 30 with type diversity from 150+), page content excerpt (≤2000 tokens), tab classification
- **Terminal adapter** — recent terminal output, detected error blocks

Output is a `TaskContext` with a complexity score (0.0–1.0) and workspace mode classification: `coding_debugging`, `reading_docs`, `browsing`, `terminal_errors`, or `mixed`.

Only workspace context (file paths, error messages, tab titles) is sent to the LLM. No biometric data ever leaves the device.

---

## L4: LLM Engine

The LLM receives the current state + confidence + workspace context and returns a structured `InterventionPlan`.

### Backends

| Mode | Config | Notes |
|------|--------|-------|
| `azure` | `CORTEX_LLM__MODE=azure` | Azure OpenAI (recommended) |
| `local` | `CORTEX_LLM__MODE=local` | Ollama on localhost:11434 |
| `rule_based` | `CORTEX_LLM__MODE=rule_based` | Built-in heuristics, no API |

### What the LLM generates

- **Situation summary** — 1-2 sentences explaining why it triggered, referencing observable workspace behavior
- **Headline** — ≤15-word focus instruction
- **Micro-steps** — 1-3 concrete actions
- **Tab recommendations** — keep/close/group per tab with relevance scores
- **Error analysis** — root cause, minimal edit, pre-crafted search query
- **UI plan** — which chrome elements to dim, whether to fold code, overlay style

### Contextual Bandit (LinUCB)

A contextual bandit selects the optimal intervention arm from 7 options based on learned user preferences:

| Arm | When |
|-----|------|
| `overlay_only` | Default HYPER |
| `simplified_workspace` | High complexity |
| `guided_mode` | Overwhelmed with mixed signals |
| `breathing` | Screen apnea detected |
| `active_recall` | Zombie reading detected |
| `circuit_breaker` | Sustained high stress integral |
| `none` | Low confidence — bandit learns when to stay quiet |

---

## L5: Intervention Engine

### Safety Guarantees

- **Consent ladder** — 5-level trust per action type (OBSERVE → SUGGEST → PREVIEW → REVERSIBLE_ACT → AUTONOMOUS_ACT). 5 approvals escalate, 3 rejections de-escalate.
- **Pre-intervention snapshot** — workspace state is captured before any change
- **Staleness check** — tab context older than 30 seconds is re-fetched before acting
- **Recently-visited protection** — tabs activated in the last 5 minutes are never closed
- **Minimum tab count** — tab close actions blocked when ≤3 tabs are open
- **Auto-restore** — workspace is restored if the intervention times out (5 min) or the user dismisses

### Intervention Actions

| Action | What It Does |
|--------|-------------|
| `close_tab` | Closes distraction tabs (saves URL for undo) |
| `group_tabs` | Groups related tabs into named, collapsed groups |
| `bookmark_and_close` | Bookmarks then closes a tab |
| `open_url` | Opens URL in a background tab |
| `search_error` | Opens Google with a pre-built error query |
| `highlight_tab` | Switches to a specific tab |
| `save_session` | Saves all tab URLs to storage |
| `start_timer` | Sets a break timer with notification |

### Learning Loop

After each intervention, the helpfulness tracker computes a reward signal from:
- Recovery detection (40%)
- Workspace complexity reduction (15%)
- Explicit user rating (30%)
- Implicit engagement signals (15%)

The bandit updates its arm weights using this reward. Over time, Cortex learns which intervention types work best for you in each context.

---

## Activity Tracker

Cortex tracks learning progress across:

YouTube · Bilibili · Coursera · LeetCode · PDFs · Jupyter Notebooks · GitHub · Stack Overflow

When you return to a session, a one-click resume card appears that seeks the video to where you left off, scrolls to your position, or pastes saved code.

---

## LeetCode Mode

Cortex has a dedicated mode for competitive programming that activates when it detects a LeetCode session:

- **Stage inference** — detects READ / PLAN / IMPLEMENT / DEBUG / REFLECT from DOM state
- **Amygdala hijack detection** — catches panic-coding patterns (rapid submit → fail cycles)
- **Lockout** — blocks further submissions during detected hijack states to force reflection
- **Pattern ladder hints** — surfaces relevant algorithmic patterns based on problem tags
- **Submission discipline guard** — warns before submitting without test coverage

---

## Graceful Degradation

| Condition | Fallback |
|-----------|---------|
| Poor lighting / face not visible | Telemetry-only state classification (stricter thresholds) |
| LLM unavailable | Rule-based intervention templates |
| Redis not running | In-memory storage |
| Camera permission denied | Daemon starts in telemetry-only mode |
