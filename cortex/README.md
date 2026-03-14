# Cortex

**Cortex** is a real-time biofeedback engine that watches you work — through your webcam and input devices — and actively intervenes when it detects cognitive overwhelm. It analyzes your workspace, generates executable actions via LLM, and restructures your digital environment so you can get back to focused work with one click.

---

## How It Works

Cortex runs a five-layer pipeline at 30 FPS, entirely on your machine:

1. **Bio-Extraction** — extracts heart rate, HRV, and respiratory rate from your face via rPPG (no camera storage), tracks blink rate, head pose, and posture via MediaPipe, and monitors mouse/keyboard patterns via pynput.
2. **State Classification** — fuses signals into a cognitive state score every 500ms. Uses rule-based scoring with EMA smoothing, hysteresis, focus-graph thrashing analysis, and screen-apnea detection to classify you as FLOW, HYPER, HYPO, or RECOVERY.
3. **Context Engine** — when an intervention is warranted, gathers workspace context: open file + diagnostics from VS Code, active tab + content from Chrome, recent terminal output. Tabs are pre-filtered (30 cap with type-diverse sampling) to fit LLM context windows.
4. **LLM Engine** — sends workspace context (no biometrics) to the configured LLM backend — Azure OpenAI, Remote Qwen-3-8B (via SSH tunnel), or local Ollama. Backend selected by `LLM_MODE` in config. The model returns a structured intervention plan: headline, micro-steps, causal explanation, suggested actions, error analysis, and per-tab recommendations. A contextual bandit selects the optimal intervention type per context.
5. **Intervention Engine** — validates and executes the plan through a pluggable adapter registry: closes distraction tabs, groups related tabs, folds irrelevant code in VS Code, shows an overlay with one-click actions. All interventions are gated by a consent ladder. Snapshots workspace state first. All actions are reversible via undo.

---

## States

| State | Meaning | Trigger |
|-------|---------|---------|
| **FLOW** | Focused, productive | Baseline |
| **HYPER** | Overwhelmed, thrashing, stuck | High HR + mouse jerk + window switching + errors |
| **HYPO** | Disengaged, drifting | Low blink rate + inactivity + flat telemetry |
| **RECOVERY** | Returning to focus | Transitioning out of HYPER/HYPO |

Interventions trigger on HYPER with confidence > 0.85, workspace complexity > 0.7, sustained for 15+ seconds (not transient spikes), with a 60-second cooldown between triggers. Minimum 5 tabs must be open for tab interventions. Progressive quiet mode (15 → 30 → 60 min) activates after repeated dismissals.

---

## Active Interventions

When Cortex detects you need help, the LLM analyzes your full workspace context and generates specific, executable actions. A contextual bandit (LinUCB) selects the best intervention type based on learned user preferences. The intervention overlay appears in the bottom-right of your active tab.

### Intervention Types

The bandit selects from these arms based on context and learned reward signals:

| Arm | When It Fires | What It Does |
|-----|---------------|-------------|
| `overlay_only` | Default HYPER | Standard tab/workspace cleanup overlay |
| `simplified_workspace` | High complexity | Aggressive workspace simplification |
| `guided_mode` | Mixed/overwhelmed | Micro-step task decomposition |
| `breathing` | Screen apnea detected | 4-7-8 breathing guide overlay |
| `active_recall` | Zombie reading detected | Fill-in-the-blank comprehension test |
| `circuit_breaker` | Sustained stress | Break recommendation with stress data |
| `none` | Low confidence | No intervention (bandit learns when to stay quiet) |

### What the LLM generates

**Causal Explanation** — every intervention includes a 1-2 sentence explanation of *why* it was triggered (e.g., "Your heart rate spiked 18% above baseline while switching between 6 tabs in 30 seconds").

**Suggested Actions** — 1-5 concrete actions per intervention:

| Action | What It Does |
|--------|-------------|
| `close_tab` | Closes distraction tabs (saves URL for undo) |
| `group_tabs` | Groups related tabs into named, collapsed groups |
| `bookmark_and_close` | Bookmarks a tab then closes it |
| `open_url` | Opens a URL in a background tab |
| `search_error` | Opens Google with a pre-built error query |
| `highlight_tab` | Switches to a specific tab |
| `save_session` | Saves all tab URLs/titles to storage |
| `copy_to_clipboard` | Copies text to clipboard |
| `start_timer` | Sets a break timer with notification |

**Error Analysis** — when terminal/editor errors are detected:
- Error type classification (syntax, import, runtime, build, test)
- Root cause category and failing abstraction identification
- Symbol location and minimal edit suggestion
- Concrete suggested fix
- Pre-crafted search query

**Tab Recommendations** — when 4+ tabs are open, every tab is assessed:
- `keep` / `close` / `group` / `bookmark_and_close` per tab
- Relevance score against your focus goal
- Group name for related tabs

### Safety

- **Consent ladder** — actions are gated by a progressive trust system. Cortex starts at SUGGEST level and earns autonomy through repeated user approvals. 3 rejections de-escalate. 5 consent levels: OBSERVE → SUGGEST → ASK_BEFORE_ACT → ACT_AND_NOTIFY → AUTONOMOUS_ACT
- **Validate-before-execute** — every tab action checks the tab still exists and hasn't navigated away (30s staleness limit on context snapshots)
- **Recently-visited protection** — tabs activated within the last 5 minutes are automatically protected from closing. The LLM receives `last_activated_ago_seconds` per tab and is instructed to keep recently-used tabs
- **Goal-aware classification** — tabs whose titles match focus goal keywords get `goal_relevant` type, which the LLM must keep with relevance ≥ 0.95. AI assistants (Gemini, ChatGPT, Claude) are reclassified as goal-relevant when title matches the goal
- **Smart tab hiding** — simplified workspace mode never hides AI assistants, documentation, learning platforms, code hosts, or recently-active/goal-relevant tabs
- **Minimum tab count** — tab interventions only fire when 5+ tabs are open
- **Tab index stabilization** — the tab list from context gathering is saved and reused when the intervention arrives, so the LLM's tab_index values always map to the correct Chrome tabs even if tabs changed between context capture and execution
- **Tab targeting by index** — LLM references tabs by integer index from the context list, never by URL (prevents hallucination)
- **Context overflow protection** — 150+ tabs are filtered to 30 with type-diverse sampling
- **Full undo stack** — all destructive actions are reversible (FIFO, max 50 entries)

---

## v2.0 Detectors

Beyond the core HYPER/HYPO classification, Cortex v2.0 runs specialized detectors that trigger targeted interventions:

### Stress Integral (Biological Pomodoro)

Replaces arbitrary 25-minute timers with biology-driven break detection. Continuously integrates HRV suppression: `L += (hrv_baseline - hrv_current) * dt`. When the accumulated stress load crosses a dynamic threshold (adjusted by the longitudinal tracker's sensitivity multiplier), Cortex flags that a break is needed. You can ride deep FLOW indefinitely until your biology says stop.

### Zombie Reading Detector

Detects passive reading — staring at text without absorbing content. Triggers when: HYPO state + browser active + low mouse velocity (<30 px/s) + elevated blink rate (>115% baseline) sustained for 90+ seconds. Fires an Active Recall intervention: a fill-in-the-blank comprehension question generated from the visible page text.

### Rabbit Hole Detector

Detects goal drift — when you've wandered far from your session goal. Compares active file/tab titles against goal keywords. Triggers after 10+ minutes below 30% alignment. Only fires in FLOW/HYPO (not HYPER, where you're already struggling). Suggests bringing back recently on-task files.

### Screen Apnea Detector

Detects breath-holding during intense focus. Uses respiratory rate extracted from the BVP signal (Butterworth bandpass 0.15–0.4 Hz + Welch PSD). Triggers when respiration drops below 8 bpm while blink suppression indicates visual fixation. Fires a breathing overlay with guided 4-7-8 pattern.

### Shutdown Detector (End-of-Day Handover)

Detects when you should stop working based on compound fatigue signals: posture collapse (>0.6), HRV drop (<70% baseline), error rate (3+ per 5 min), late hour (10 PM+). Requires 2/3 signals sustained for 5+ minutes. Triggers a handover snapshot — captures full workspace state (editor, terminal, browser, git diff) and writes a Markdown brief to `storage/handovers/`. Next morning, a briefing notification shows where you left off.

### Focus Transition Graph

Builds a directed graph of app/tab focus transitions in real time. Computes a thrashing score from: node diversity (30%), switch velocity (30%), dwell time (25%), and revisit ratio (15%). The thrashing score feeds into state classification, replacing simple window-switch frequency.

### Longitudinal Tracker

Tracks physiological baselines (HR, HRV, respiration) over days and weeks. Detects trends via linear regression on daily HRV snapshots. Declining HRV → sensitivity multiplier drops (0.5–1.0), triggering breaks sooner. Improving HRV → multiplier rises (1.0–1.5), fewer interruptions. Hourly snapshots run automatically.

---

## LeetCode Mode

Cortex v2.1 adds a domain-specific pipeline for competitive programming on LeetCode. A content script observes the LeetCode DOM at 1 Hz, three biological detectors analyze your physiological response to problem-solving, a mode resolver maps signals to intervention decisions, and five concrete interventions gate your workflow.

### Observer (Browser Extension)

The `LeetCodeObserver` content script extracts problem-solving context in real time:

- **Problem metadata** — title, difficulty, tags, submission history (via resilient multi-selector DOM scraping)
- **Code telemetry** — rolling 60-second keystroke window tracking insertions/deletions and chars/min via Monaco/CodeMirror APIs
- **Behavioral signals** — reread count (scroll-back detection), solutions tab attempts
- **Stage inference** — classifies your current phase:
  - `READ` — first 60s or editor unfocused
  - `PLAN` — editor focused + <20 chars/min
  - `IMPLEMENT` — editor focused + ≥20 chars/min
  - `DEBUG` — post-submission with wrong answer
  - `REFLECT` — post-accept

Emits `LEETCODE_CONTEXT_UPDATE` every second. Saves session state to `chrome.storage.local` on tab close. Handles SPA navigation via URL polling.

### Biological Detectors

| Detector | What It Detects | Formula / Conditions | Threshold |
|----------|----------------|---------------------|-----------|
| **Amygdala Hijack** | Acute emotional flooding after Wrong Answer | `AAI = 0.4·max(0, dHR/dt) - 0.3·ΔBlinks(t) + 0.3·Velocity_keys(t)` | AAI > 0.7 within 5s of WA |
| **Destructive Struggle** | Productive→destructive transition | Path 1: reread > 2 + dwell > 5min + rising allostatic load. Path 2: WA > 2 in 10min + delete ratio > 0.5 + HRV < 80% baseline | Either pathway |
| **Parasympathetic Rebound** | Optimal learning window post-accept | Problem accepted + HR within 5% of baseline + HRV rising | All conditions met |

### Mode Resolver

Combines cognitive state + detector outputs into a `LeetCodeModeEstimate` (stage × mode). Priority order:

| Mode | Trigger | Priority |
|------|---------|----------|
| `PANIC` | WA count ≥ 4 AND stress_integral ≥ 400 | Highest |
| `AMYGDALA_HIJACK` | AAI > 0.7 | High |
| `DESTRUCTIVE_STRUGGLE` | Either detector pathway active | Medium |
| `FATIGUE` | Allostatic load > 400 | Medium |
| `PRODUCTIVE_STRUGGLE` | Active struggle, not destructive | Low |
| `FLOW` | Baseline | Lowest |

### Interventions (Stage × Mode Matrix)

| Intervention | Stage × Mode | What It Does |
|-------------|-------------|-------------|
| **Restatement Scratchpad** | READ/PLAN × DESTRUCTIVE_STRUGGLE | Opens a scratchpad prompting you to restate the problem in your own words. 5-minute cooldown. |
| **Pattern Ladder** | PLAN/IMPLEMENT × PRODUCTIVE_STRUGGLE | 4-level progressive hint system (category → technique → pseudocode → code skeleton). 2-minute cooldown. |
| **Amygdala Lockout** | DEBUG × AMYGDALA_HIJACK | Locks editor for 90s (escalates +30s per WA above 3, capped at 180s). Forces a physiological reset before retrying. |
| **Submission Discipline Guard** | IMPLEMENT/DEBUG × any | Fires when WA > 2. Gates submission with a checklist requiring you to articulate what changed. |
| **Solution Escape Friction** | any × PANIC/FATIGUE | Intercepts solutions tab navigation. Adds deliberate friction (60s cooldown) before allowing access. |

### Longitudinal (LeetCode)

Tracks per-session problem metrics and per-tag skill growth:

- **Session metrics** — problems attempted/accepted, panic episodes, lockout count, solution escapes, pattern ladder depth, peak allostatic load
- **Skill metrics** — per-tag (e.g., "dynamic-programming", "trees") attempt/accept counts and acceptance rate
- **Daily load budget** — 600 units; triggers session-end recommendation when exceeded
- Midnight rollover resets session metrics while preserving skill metrics

### Adapter

The `LeetCodeAdapter` bridges the runtime to the browser extension over WebSocket. Supports 15 action types including `lock_editor`, `intercept_submit`, `gate_solutions`, `show_scratchpad`, `show_pattern_ladder`, `show_lockout`, `show_consolidation`, `show_submission_gate`, `show_solution_friction`, and `show_session_briefing`. Also proxies AI-powered checks: restatement, comprehension, hypothesis, stuck analysis, and session briefing.

---

## Learning Loop

Cortex learns from every intervention to get better over time:

1. **Helpfulness Tracker** — captures pre/post intervention state and computes a reward signal from implicit signals (was it undone? ignored? engaged with?) and explicit signals (thumbs up/down rating). Reward = recovery weight (40%) + complexity reduction (15%) + explicit rating (30%) + implicit signals (15%).

2. **Contextual Bandit (LinUCB)** — selects which intervention type to deploy based on 8 context features: state code, complexity, tab count, error count, hour of day, thrashing score, stress integral, and consent level. Updates A matrices and b vectors after each intervention. Persists weights to store every 10 updates.

3. **Tab Relevance Tracker** — learns per-domain relevance from user feedback on tab close recommendations. Uses exponential moving average (α=0.3) to update domain scores: keeping a tab → relevant (1.0), confirming a close → irrelevant (0.0). Per-tab feedback: when the user uses Keep buttons on individual tabs in the overlay, each tab's kept/closed decision is recorded separately (not all-or-nothing). Scores persist with 90-day TTL and are scoped per focus goal. Personalizes which tabs Cortex recommends closing.

4. **Replay Harness** — offline A/B testing. Load JSONL session recordings and replay them through alternative scoring policies and prompt configurations. Compare baseline vs. variant on reward delta, engagement delta, and intervention count.

```bash
# Offline evaluation
python -m cortex.scripts.replay_harness --scorer v2 --prompts v2 sessions/*.jsonl

# Batch bandit training
python -m cortex.services.eval.bandit_trainer --data sessions/ --output models/
```

---

## Chrome Extension

Built with Plasmo + React (Manifest V3). Lives in `apps/browser_extension/`.

### Popup Dashboard

Dark, high-end interface (Linear/Raycast-inspired) showing:
- **Launch Cortex / Restart Camera** button — connects to the daemon and enables webcam capture in one click. Shows "Launch Cortex" when disconnected, "Restart Camera" when connected (re-sends `webcam_enabled` to restart the capture pipeline). Displays inline error with `cortex-dev` instructions if the daemon isn't running.
- Connection status with live cognitive state indicator (FLOW/HYPER/HYPO/RECOVERY)
- Morning briefing card ("Where you left off" summary from yesterday's handover)
- Focus session controls with goal input and Enter-to-start
- Big number display of real focus minutes with color-coded progress bar
- Current streak timer, distractions blocked, and longest streak
- Live biometrics (BPM, HRV, blink rate) in monospace
- Active intervention preview: causal explanation, tab close list, error analysis card, one-click CTA, undo
- Thumbs up/down rating buttons for intervention feedback
- Daily stats grid (total focus, sessions, best streak, distractions blocked)
- Health alerts and break suggestions appear as dismissible cards

### Intervention Overlay

Injected via Shadow DOM into the active tab:
- Causal explanation (italicized, explains *why* this intervention fired)
- Tab close list with red `x` marks
- "Keeping N you need" count
- Error analysis with monospace suggested fix
- Single CTA button that executes all recommended actions
- Undo link to reverse all changes
- Auto-dismisses 1.5s after action execution

### Dismissal Cooldown

When you dismiss an intervention (click Dismiss, X, backdrop, or press Escape), Cortex enforces two cooldown layers to prevent the popup from reappearing:

- **Intervention ID cooldown** — the same intervention won't re-trigger for 30 minutes
- **URL-based cooldown** — no intervention will fire for the same hostname for 10 minutes
- **Active guard** — if an intervention is already showing, all incoming triggers are dropped
- Old cooldown entries are pruned automatically

### Breathing Overlay

Full-screen guided breathing when screen apnea is detected:
- 4-7-8 breathing pattern (inhale 4s, hold 7s, exhale 8s)
- Visual animation synced to breath phases

### Active Recall Overlay

Comprehension test when zombie reading is detected:
- Scrapes visible page text via `scrapeVisibleText()`
- LLM generates fill-in-the-blank question from content
- Tests whether you're actually absorbing what you're reading

### Ambient Somatic Feedback

Sub-threshold content script running on every page. Receives `AMBIENT_STATE_UPDATE` from the background service worker every 2 seconds and updates four visual layers:

- **Aura** — barely-visible radial vignette at screen edges, color shifts with cognitive state (emerald when focused, red when stressed, blue when disengaged). Max 3% opacity at edges, 3-second transitions.
- **Somatic filter** — full-screen color temperature overlay using `mix-blend-mode: multiply`. Cool blue tint during focus (1.5% opacity), warm amber during stress (3.5% opacity). 45-second transitions so changes are imperceptible.
- **Weather particles** — canvas at 15fps with state-dependent particle count. HYPER: 35 rain-like vertical streaks falling from top. FLOW: 6 gentle floating dots. Particles are 3-7% opacity.
- **Flow Shield** — during FLOW state, gradually fades known distraction elements (YouTube recommendations, Twitter trends, Reddit sidebars, GitHub feed) to 5% opacity over 3 minutes. Saves original opacity and fully restores on state change. Targets site-specific CSS selectors.

### Pulse Room (New Tab)

Replaces new tab with a dark canvas visualization:
- Central orb pulses at your actual heart rate
- Ripple rings expand on each beat
- ECG-style trace with scanning dot
- Monospace BPM readout

### Focus Sessions

- Start with an optional goal ("Studying PyTorch CUDA debugging")
- Tracks real focus minutes, focus percentage, current/best streaks
- Blocks distraction sites (Reddit, Twitter/X, YouTube, Facebook, Instagram, TikTok, Netflix, Twitch, Discord) with an overlay interceptor showing your stats. Clicking "Continue" removes the overlay and reveals the original page (no reload). Clicking "Go back" navigates away. Distraction counter only increments on "Go back". Goal-relevant content on YouTube/Reddit (title matches goal keywords) bypasses the block.
- Focus goal flows through the entire pipeline to inform LLM tab relevance scoring

### Activity Tracker

Universal content script (`activity-tracker.ts`) that tracks learning progress across all platforms and enables one-click resume.

**Supported platforms:**

| Platform | Position Type | What It Tracks |
|----------|---------------|----------------|
| YouTube, Bilibili | video | timestamp, duration, chapter, playlist position |
| Coursera, edX, Khan Academy, Udemy | video | timestamp, duration (course lecture context) |
| HackerRank, Codeforces | code_problem | stage, wrong answers, code snapshot |
| Jupyter, Google Colab | notebook | cell index, scroll position |
| PDF viewers (Chrome, pdf.js) | pdf | page number, total pages |
| Google Slides, reveal.js | slides | slide index, total slides |
| Docs, articles, blogs (fallback) | scroll | scroll percentage, max reached |

**How it works:**
- Detects platform via URL + DOM inspection, polls position every 5s
- Canonical URL normalization (strips tracking params, normalizes YouTube/Bilibili/LeetCode URLs)
- SPA navigation detection: YouTube custom events + 2s URL polling + visibilitychange + beforeunload
- Dwell time tracking: only accumulates when page is visible, avoids double-counting across 5s updates
- LeetCode is handled by the existing LeetCodeObserver — the activity tracker bridges session data automatically

**Exclusions:** Videos < 60s, live streams, `chrome://` URLs, login/auth pages, search engines, incognito mode, dwell < 120s.

**Resume Card:** When you return to tracked content after > 1 hour, a Shadow DOM card appears (bottom-right, 300px). Shows platform, title, position with progress bar, and a "Resume" button that actively restores your position:
- Video: seeks to saved timestamp (verifies same content via duration check)
- Scroll: smooth scrolls to saved position
- Code: pastes saved code snapshot into Monaco editor
- PDF: jumps to saved page via PDFViewerApplication or URL hash
- Notebook: scrolls to saved cell
- Slides: navigates to saved slide

Auto-dismisses after 15s. ESC key or scroll dismisses. "Dismiss" marks the activity so the card won't reappear.

**New Tab:** The Pulse Room shows up to 3 recent incomplete activities below the heartbeat visualization. Each shows title, position, and time ago. Clicking navigates with timestamp in URL for videos.

**Daemon integration:** ActivityAggregator stores daily timelines (90-day TTL). ActivitySummarizer generates LLM recaps. Activity data feeds into handover snapshots and morning briefings.

### Tab Closing Toggle

Toggle switch in the popup settings card. When disabled, all `close_tab` and `bookmark_and_close` actions are blocked — Cortex still suggests which tabs to close but won't execute the closes. Persisted in `chrome.storage.local`, survives browser and extension restarts. Re-enabling immediately restores full tab management capability.

### Tab Classification

Every tab is classified by URL pattern into: `documentation`, `stackoverflow`, `pdf`, `paper`, `reference`, `search`, `code_host`, `learning_platform`, `ai_assistant`, `video_platform`, `communication`, `social`, `distraction`, or `other`. During a focus session, goal-aware classification reclassifies ambiguous types (`video_platform`, `social`, `communication`, `distraction`, `ai_assistant`, `other`) as `goal_relevant` when the tab title contains focus goal keywords — including short tech terms ("Go", "ML", "AI", "CSS", "SQL", "Vue") via a curated allowlist. Goal-relevant tabs receive the strongest LLM protection (relevance ≥ 0.95, never close).

### Health Alerts

- **Eye strain** — if blink rate stays below 10/min for 3 minutes, shows a 20-20-20 rule reminder (look 20ft away for 20 seconds). 5-minute cooldown between alerts.
- **Posture** — if forward lean exceeds threshold for 3 minutes, shows a posture correction notification. 5-minute cooldown.
- **Break recommendations** — biology-driven via stress integral (replaces static timers). Shows a toast with specific reason (e.g., "Your HRV has been suppressed for 40 minutes").

---

## VS Code Extension

Built with TypeScript. Lives in `apps/vscode_extension/`.

- Provides active file, diagnostics, and symbol at cursor
- Receives fold commands from the intervention engine to collapse irrelevant code sections
- **Morning briefing** — shows notification with summary and action items from yesterday's handover on startup
- **Copilot throttle** — disables inline suggestions (VS Code + GitHub Copilot) during HYPER state, re-enables in FLOW. Reduces cognitive noise when overwhelmed
- Commands: `cortex.disableInlineSuggestions`, `cortex.enableInlineSuggestions`

---

## Consent Ladder

Cortex uses a progressive trust system to gate intervention autonomy. Every action type starts at a low consent level and earns autonomy through repeated user approvals.

| Level | Name | Behavior |
|-------|------|----------|
| 0 | OBSERVE | Cortex watches, no interventions |
| 1 | SUGGEST | Shows overlay, user must click to execute |
| 2 | ASK_BEFORE_ACT | Asks permission, then executes on approval |
| 3 | ACT_AND_NOTIFY | Executes immediately, shows notification |
| 4 | AUTONOMOUS_ACT | Executes silently |

- 5 consecutive approvals → escalate one level
- 3 rejections → de-escalate one level
- Global max level cap (user-configurable)
- Per-action type tracking (e.g., `close_tab` may reach level 3 while `group_tabs` stays at level 1)
- State persists to store (Redis or in-memory)

---

## Project Launcher

Zero-friction project onboarding. Define launch profiles in YAML with VS Code workspace, Chrome URLs, terminal commands, apps to hide, focus goal, and screen layout. One command opens everything.

```bash
# List configured projects
curl http://127.0.0.1:9472/api/projects

# Launch a project
curl -X POST http://127.0.0.1:9472/api/launch/my-project
```

---

## Storage

Dual-backend persistence layer (Redis with automatic in-memory fallback). All services persist state, weights, and metrics without a hard dependency on Redis running.

- **Redis store** — async Redis client using sorted sets for timeseries, JSON serialization for state. Auto-falls back to in-memory on connection failure.
- **In-memory store** — dict-backed with deque-based timeseries. Used when Redis is unavailable or disabled.
- Helpfulness records persist with 90-day TTL
- Bandit weights persist every 10 updates
- Consent ladder state persists across restarts
- Longitudinal daily baselines persist indefinitely

---

## Adapter Registry

Pluggable architecture for workspace integrations. Adapters implement the `CortexAdapter` protocol (name, capabilities, execute, get_context, health_check). The registry handles discovery, capability querying, action routing, and health checks. Supports plugin discovery via Python entry points. Legacy adapters are auto-wrapped for backward compatibility.

---

## Setup

**Requirements:** Python 3.11+, macOS (primary target), webcam, Azure OpenAI deployment, Node.js 18+, pnpm. Optional: Redis 7+ (falls back to in-memory).

```bash
cd /path/to/Ralph

# Install
pip install -e "./cortex[dev]"
export PYTHONPATH="$PWD"

# Copy and edit config
cp cortex/.env.example .env

# Initialize storage and default config
python -m cortex.scripts.seed_config --root .

# Calibrate personal baselines (2 min)
cortex-calibrate

# Start everything
cortex-dev

# Optional desktop shell
python -m cortex.apps.desktop_shell.main
```

REST API runs at `http://127.0.0.1:9472`. WebSocket runs at `ws://127.0.0.1:9473`.

### Chrome Extension

```bash
cd cortex/apps/browser_extension
pnpm install
npx plasmo build

# Load in Chrome:
# 1. Open chrome://extensions
# 2. Enable Developer mode
# 3. Click "Load unpacked"
# 4. Select build/chrome-mv3-prod/
```

### Edge Extension

```bash
cd cortex/apps/browser_extension
pnpm install
npx plasmo build --target=edge-mv3

# Load in Edge:
# 1. Open edge://extensions
# 2. Enable Developer mode
# 3. Click "Load unpacked"
# 4. Select build/edge-mv3-prod/
```

Dev/build/package scripts: `pnpm dev:edge`, `pnpm build:edge`, `pnpm package:edge`. Tab group APIs have graceful fallback for Edge quirks (collapse may silently fail). All other APIs (storage, tabs, scripting, webNavigation) work identically.

### Native Messaging Host (Auto-Launch Daemon)

Allows the Chrome extension to automatically start the Cortex daemon when it connects:

```bash
# One-time setup: register native messaging host with Chrome
python -m cortex.scripts.install_native_host

# Or specify extension ID manually
python -m cortex.scripts.install_native_host --extension-id abcdef1234567890
```

The host (`native_host.py`) uses Chrome's native messaging protocol (4-byte length-prefixed JSON over stdio). On "launch" command, it spawns the daemon as a detached process and waits up to 8 seconds for port 9473 readiness. On "status" command, it checks if the daemon is already running.

### Testing Interventions

A standalone test script sends a mock intervention without the full daemon:

```bash
python -m cortex.scripts.test_intervention
# Starts ws://127.0.0.1:9473, sends INTERVENTION_TRIGGER on extension connect
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/state` | Current cognitive state estimate |
| GET | `/api/stress-integral` | Current stress load, threshold, and break recommendation |
| GET | `/api/helpfulness/summary` | Intervention helpfulness metrics |
| GET | `/api/projects` | List configured project launch profiles |
| POST | `/api/launch/{name}` | Launch a project workspace |
| WS | `ws://127.0.0.1:9473` | Real-time state, interventions, briefings |

---

## Signals & Weights

| Signal | Weight | How It's Measured |
|--------|--------|-------------------|
| Pulse elevation | 20% | rPPG from forehead/cheek ROI vs. personal baseline |
| HRV drop | 15% | RMSSD from inter-beat intervals |
| Blink suppression | 12% | Eye Aspect Ratio below threshold for extended period |
| Mouse thrashing | 15% | Velocity variance + jerk score |
| Thrashing (focus graph) | 15% | Focus transition graph: diversity, velocity, dwell, revisit |
| Workspace complexity | 15% | Diagnostic count + tab count + context density |
| Posture collapse | 8% | Shoulder drop ratio + forward lean angle |

Additional signals (not weighted into state score, used by detectors):
- **Respiratory rate** — BVP-derived via Butterworth bandpass + Welch PSD (screen apnea detector)
- **Stress integral** — cumulative HRV suppression over time (biological pomodoro)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Webcam (30 FPS)                       │
└──────────────────────────┬──────────────────────────────────┘
                           │
          ┌────────────────▼─────────────────┐
          │  L1: Bio-Extraction              │
          │  rPPG · Respiration · Blink ·    │
          │  Pose · Telemetry                │
          └────────────────┬─────────────────┘
                           │  FeatureVector (500ms, 14-dim)
          ┌────────────────▼─────────────────┐
          │  L2: State Engine                │
          │  Fusion · Focus Graph · Scoring  │
          │  · Smoothing · Detectors         │
          └────────────────┬─────────────────┘
                           │  StateEstimate + stress_integral
          ┌────────────────▼─────────────────┐
          │  L3: Context Engine              │
          │  VS Code · Chrome · Terminal     │
          │  Adapter Registry                │
          └────────────────┬─────────────────┘
                           │  TaskContext
          ┌────────────────▼─────────────────┐
          │  L4: LLM Engine                  │
          │  Azure OpenAI · Qwen-3 · Ollama │
          │  Contextual Bandit arm selection  │
          └────────────────┬─────────────────┘
                           │  InterventionPlan + causal_explanation
          ┌────────────────▼─────────────────┐
          │  L5: Intervention Engine         │
          │  Consent · Validate · Execute ·  │
          │  Undo · Helpfulness Tracking     │
          └──────────────────────────────────┘
                           │
          ┌────────────────▼─────────────────┐
          │  Store (Redis / In-Memory)       │
          │  Weights · State · Timeseries    │
          └──────────────────────────────────┘
```

All layers communicate via the FastAPI gateway (`api_gateway/`) and WebSocket server on port 9473. The desktop shell, VS Code extension, and Chrome extension are all clients of this WebSocket.

### LLM Prompt Modes

| Mode | Trigger | Output |
|------|---------|--------|
| `debug_error_summary` | Terminal/editor errors | Error analysis + search action + docs link |
| `code_focus_reduction` | Coding with too much visible code | `close_tab` actions for distractions |
| `browser_tab_reduction` | 5+ tabs open | Per-tab recommendations + close/group actions |
| `micro_step_planner` | Mixed/overwhelmed state | Actions for automatable steps |
| `calm_overlay_writer` | Reading docs / mild state | Actions for obviously irrelevant tabs |
| `breathing_overlay` | Screen apnea detected | 4-7-8 breathing guide |
| `active_recall` | Zombie reading detected | Fill-in-the-blank comprehension question |
| `rabbit_hole` | Goal drift >10 min | Goal reminder + workspace rearrangement |
| `alignment_summary` | High thrashing score | Focus transition analysis |
| `deep_bottleneck_diagnosis` | Complex debugging | Failing abstraction isolation + minimal edit |

---

## Privacy

- **No video is ever saved.** Frames are processed in memory and immediately discarded.
- **No biometrics reach the LLM.** The model sees only workspace context: file paths, error messages, tab titles. Heart rate, HRV, respiration, blink data, and posture angles never leave your machine.
- **Consent-gated autonomy.** No action executes without earned trust. Users control the maximum autonomy level.
- **Minimal browser permissions.** The Chrome extension requests `activeTab`, `scripting`, `tabs`, `tabGroups`, `storage`, `alarms`, and `bookmarks`. It does not request browsing history.
- **Local sensing, cloud planning.** The only network traffic is the LLM call, and Cortex sends workspace text context only.

---

## Project Structure

```
cortex/
├── libs/
│   ├── adapters/            # CortexAdapter protocol, AdapterRegistry, plugin discovery,
│   │                        #   LeetCodeAdapter (WebSocket bridge to browser extension)
│   ├── config/              # CortexConfig, RedisConfig, defaults.yaml, .env loading
│   ├── schemas/             # Pydantic models (state, context, intervention, consent,
│   │                        #   eval, longitudinal, transition_graph, actions, leetcode, activity)
│   ├── store/               # RedisStore + InMemoryStore (auto-fallback)
│   ├── signal/              # Butterworth filters, Welch PSD, windowing
│   ├── logging/             # structlog JSON event logging
│   └── utils/               # Platform detection, async helpers, secrets
├── services/
│   ├── capture_service/     # Webcam capture, MediaPipe face tracking, quality gating
│   ├── physio_engine/       # POS/CHROM rPPG, BVP peak detection, HR/HRV, respiration
│   ├── kinematics_engine/   # EAR blink detection, solvePnP head pose, shoulder posture
│   ├── telemetry_engine/    # pynput input hooks, window tracker, focus graph, aggregation
│   ├── state_engine/        # Feature fusion, rule scorer, EMA smoother, trigger policy,
│   │                        #   stress integral, longitudinal, zombie detector, rabbit hole,
│   │                        #   amygdala hijack, destructive struggle, parasympathetic rebound,
│   │                        #   LeetCode mode resolver, LeetCode longitudinal tracker
│   ├── context_engine/      # Editor, browser, terminal adapters + app classifier
│   ├── llm_engine/          # Azure OpenAI client, Ollama fallback, prompts, parser, cache
│   ├── intervention_engine/ # Trigger, snapshot, planner, executor (adapter registry), restore,
│   │                        #   LeetCode interventions (lockout, scratchpad, ladder, guards)
│   ├── consent/             # ConsentPolicy + ConsentLadder (progressive trust)
│   ├── eval/                # HelpfulnessTracker, ContextualBandit (LinUCB), bandit trainer,
│   │                        #   TabRelevanceTracker (per-domain EMA learning)
│   ├── handover/            # ShutdownDetector, HandoverSnapshot, MorningBriefing
│   ├── activity_tracker/    # ActivityAggregator (daily timelines), ActivitySummarizer (LLM recaps)
│   ├── launcher/            # ProjectConfig (YAML profiles), ProjectLauncher
│   ├── throttle/            # CopilotThrottle (silence inline suggestions in HYPER)
│   ├── api_gateway/         # FastAPI REST routes, WebSocket server
│   └── runtime_daemon.py    # Main orchestrator — ties all v1 + v2 services together
├── apps/
│   ├── desktop_shell/       # PySide6: tray, dashboard, overlay, settings, onboarding
│   ├── vscode_extension/    # TypeScript: WS client, context provider, fold controller,
│   │                        #   morning briefing, copilot throttle
│   └── browser_extension/   # Plasmo/React: background SW, content script, popup, newtab,
│                            #   tab manager, ambient engine, action executor, undo stack,
│                            #   breathing overlay, active recall overlay, LeetCode observer,
│                            #   intervention dismissal cooldown, activity tracker, resume card,
│                            #   tab-close toggle
├── scripts/
│   ├── run_dev.py           # Start all services
│   ├── calibrate.py         # Capture personal baselines
│   ├── seed_config.py       # Initialize storage and config
│   ├── test_intervention.py # Mock intervention test server
│   ├── replay_harness.py    # Offline session replay and A/B evaluation
│   ├── native_host.py       # Chrome native messaging host (daemon auto-launch)
│   ├── install_native_host.py # Register native messaging host manifest
│   └── build_macos_app.sh   # macOS app packaging
├── tests/
│   ├── unit/                # Per-module unit tests (40 test files)
│   └── integration/         # Pipeline integration tests
└── docs/
    ├── setup.md
    ├── deploy_azure.md
    ├── calibration.md
    └── ...
```

---

## Development

```bash
# Run all tests
pytest

# With coverage
pytest --cov=cortex --cov-report=html

# Type check
mypy cortex/

# Lint
ruff check cortex/

# Build Chrome extension
cd cortex/apps/browser_extension && npx plasmo build

# Test intervention overlay without full daemon
python -m cortex.scripts.test_intervention

# Replay a session with alternative config
python -m cortex.scripts.replay_harness --scorer v2 --prompts v2 sessions/*.jsonl

# Train bandit offline
python -m cortex.services.eval.bandit_trainer --data sessions/ --output models/
```

---

## Docs

- [Setup](docs/setup.md) — installation, Azure config, packaging, troubleshooting
- [Azure Deployment](docs/deploy_azure.md) — deploy-and-experience checklist
- [Calibration](docs/calibration.md) — personal baseline capture and usage

---

## License

MIT
