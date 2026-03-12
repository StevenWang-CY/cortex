# Project Spec: Cortex — The Somatic Workspace Engine

## Overview

Cortex is a real-time biofeedback workspace engine that uses remote photoplethysmography (rPPG) and computer vision to continuously monitor a user's autonomic nervous system through an ordinary webcam. When Cortex detects the biological signature of cognitive overwhelm — elevated heart rate, collapsed HRV, suppressed blink rate, degraded posture, erratic input behavior — it autonomously intervenes in the user's digital workspace, leveraging a local Qwen-3-8B model running on a remote GPU to semantically restructure visible information into manageable, stress-appropriate micro-steps.

The first implementation target is **coding/debugging overwhelm**: VS Code editor, terminal, and browser documentation/StackOverflow tabs.

**Target end-to-end demo:**
> User is overwhelmed debugging code → Cortex detects overload via webcam + telemetry → gathers VS Code + terminal + browser context → Qwen-3-8B on gwhiz1 produces a minimal debugging plan → Cortex folds irrelevant code, hides clutter, shows one next step, and restores the workspace afterward.

## Tech Stack

- **Language (Core):** Python 3.11+
- **Language (Extensions):** TypeScript (Chrome + VS Code extensions, React for UI)
- **API Framework:** FastAPI (async, Pydantic-native, auto-docs)
- **Orchestration:** asyncio + multiprocessing (asyncio for IO; multiprocessing for webcam pipeline)
- **Schemas / Config:** Pydantic + YAML
- **rPPG Library:** open-rppg + pyVHR (open-rppg for real-time; pyVHR for validation/benchmarking)
- **Face / Pose:** MediaPipe 0.10+ (FaceMesh blink + Pose shoulders; CPU at 30fps)
- **Webcam:** OpenCV 4.x (cross-platform VideoCapture; threaded capture)
- **Signal Processing:** SciPy + NumPy (Butterworth filters, Welch PSD, peak detection)
- **ML Framework:** PyTorch → ONNX Runtime (train PyTorch; deploy ONNX <5ms inference)
- **LLM (Primary):** Qwen-3-8B on gwhiz1.cis.upenn.edu (already deployed on GPU)
- **LLM Serving:** vLLM or SGLang on gwhiz1 (OpenAI-compatible API)
- **LLM (Fallback):** Ollama (llama.cpp) — local REST API
- **Browser Extension:** Plasmo + React (Manifest V3; Shadow DOM; hot reload)
- **VS Code Extension:** VS Code Extension API (TypeScript)
- **Desktop UI:** PySide6 (V1) / Tauri (V2)
- **IPC:** WebSocket (`websockets` lib)
- **Input Telemetry:** pynput (cross-platform mouse/keyboard hooks)
- **Data Storage:** SQLite + Parquet + JSONL
- **Package Manager:** uv (Python), pnpm (JS)
- **Test Framework:** pytest (Python), vitest (TypeScript)

## Architecture

### Five-Layer Pipeline (Split Local/Remote)

No video data or raw physiological signals ever leave the local device. The LLM runs on a remote GPU machine. Target: sub-200ms end-to-end latency from biological signal change to state update; sub-12s from overwhelm detection to rendered intervention (including LLM generation).

| Layer | Component | Description |
|-------|-----------|-------------|
| **L1** | **Bio-Extraction Pipeline** | Webcam daemon at 30fps: rPPG heart rate/HRV extraction, MediaPipe face mesh for blink rate, pose estimation for shoulder posture, mouse/keyboard/window telemetry aggregation. |
| **L2** | **State Classification Engine** | Rule-based scorer (V1) upgradeable to lightweight TCN. Consumes time-series features from L1, classifies user into `FLOW`, `HYPO`, `HYPER`, or `RECOVERY` zones in real time. |
| **L3** | **Context Engine** | VS Code extension, Chrome extension, terminal adapter. Gathers current workspace semantic context: code, errors, docs, tabs. |
| **L4** | **LLM Scaffolding Engine** | Remote inference via Qwen-3-8B on gwhiz1.cis.upenn.edu. Analyzes workspace context and generates structured intervention payloads. |
| **L5** | **Intervention Engine & UI Interceptor** | Decides when to intervene. Chrome extension content scripts for DOM manipulation. VS Code extension for code folding. Desktop overlay for non-browser contexts. Manages intervention lifecycle and workspace restoration. |

### Data Flow

```
Webcam Frame (30fps)
    │
    ├──► Face Mesh → ROI Extraction → rPPG Buffer → Pulse/HRV Features
    ├──► Face Mesh → EAR → Blink Features
    ├──► Pose Estimation → Shoulder/Lean Features
    │
Mouse/Keyboard/Window Hooks → Telemetry Features
    │
    ▼
Feature Fusion (12-dim vector every 500ms)
    │
    ▼
State Engine (rolling buffer → classify → smooth → hysteresis)
    │
    ▼ (if HYPER + confidence > threshold + cooldown inactive)
    │
Context Engine ──► Gather VS Code + Terminal + Browser context
    │
    ▼
LLM Engine (Qwen-3-8B on gwhiz1) ──► Structured JSON intervention plan
    │
    ▼
Intervention Engine ──► Validate plan → Snapshot workspace → Apply
    │                                                         │
    ▼                                                         ▼
Monitor for recovery ◄────────────────────────── Restore workspace
```

### Build Priority Order

**Priority 1 — Core Sensing + Detection:** webcam capture, face landmarks, rPPG pulse proxy, blink/head/posture features, keyboard/mouse/active-window telemetry, real-time overwhelm classifier.

**Priority 2 — Workspace Adapters:** browser adapter, VS Code adapter, terminal adapter, overlay UI.

**Priority 3 — LLM Scaffolding:** summarize logs/code/docs, isolate one subproblem, generate micro-steps, produce a reversible simplification plan.

**Priority 4 — Stability / Restore / Config:** confidence gating, fail-safe logic, user override, intervention cooldown, workspace restore.

### Compute & Deployment

| Component | Runs On |
|-----------|---------|
| Webcam capture + face processing | Local user machine |
| Telemetry hooks (mouse/keyboard/window) | Local user machine |
| State engine (feature fusion + classification) | Local user machine |
| Context engine (workspace gathering) | Local user machine |
| Intervention engine + desktop shell + overlay UI | Local user machine |
| VS Code extension | Local user machine |
| Chrome extension | Local user machine |
| **Qwen-3-8B LLM inference** | **Remote: gwhiz1.cis.upenn.edu** |

### LLM Backend Abstraction

```python
class LLMClient(Protocol):
    async def generate_intervention_plan(
        self, context: TaskContext, state: StateEstimate, constraints: SimplificationConstraints
    ) -> InterventionPlan: ...
```

- **Primary:** Remote Qwen-3-8B on gwhiz1 via OpenAI-compatible API (vLLM/SGLang serving)
- **Fallback 1:** Local Ollama / llama.cpp with a smaller quantized model
- **Fallback 2:** Any OpenAI-compatible endpoint

### LLM Client Configuration

```python
class LLMConfig(BaseModel):
    mode: Literal["remote", "local", "openai_compat"] = "remote"
    remote_host: str = "gwhiz1.cis.upenn.edu"
    remote_port: int = 8800
    ssh_tunnel: bool = True
    ssh_user: str = "wangcy07"
    model_name: str = "qwen3-8b"
    max_tokens: int = 1024
    temperature: float = 0.3
    timeout_seconds: float = 10.0
    fallback_mode: Literal["local_ollama", "rule_based"] = "rule_based"
```

## Components

### L1: Biological Extraction Pipeline

#### capture_service

**Responsibilities:**
- Initialize webcam and acquire frames at stable FPS
- Estimate frame quality (brightness, blur, motion)
- Run face detection and landmark extraction via MediaPipe FaceMesh (468 landmarks)
- Publish normalized face ROI streams

**Inputs:** Camera device ID, Target FPS (default: 30), Capture resolution (default: 640×480, USB 2.0 compatible)

**Outputs:**
- Raw frame (memory only — NEVER saved to disk)
- Frame timestamp (monotonic)
- Face landmarks (468-point mesh)
- Face bounding box + confidence score
- Frame quality metrics: brightness_score, blur_score, motion_score

**Implementation Notes:**
- MediaPipe FaceMesh is acceptable for V1
- Do not save raw video unless explicit debug mode enabled
- Expose capture stream internally via async queue or pub/sub event bus
- Implement adaptive frame skip if overloaded
- Face lost / reacquire handling with hysteresis
- Quality checks: low light, blur, occlusion, extreme motion
- Monitor mean frame brightness; flag if < 50 lux
- Discard frames where landmark jitter > 5px inter-frame displacement at nose tip

#### physio_engine — Remote Photoplethysmography (rPPG)

Remote photoplethysmography exploits the fact that each cardiac cycle causes a transient increase in blood volume in facial capillary beds, producing microscopic fluctuations in skin reflectance detectable by a standard RGB camera. The green channel exhibits the highest correlation with the PPG signal because hemoglobin absorption peaks near 540nm.

**V1 Algorithm: POS (Plane Orthogonal to Skin)** — achieves MAE < 3 BPM on UBFC-rPPG without GPU requirements.

**Also implement as validation/fallback:**
- Green-channel baseline
- CHROM (performs better across diverse skin tones)

**V2 migration path: EfficientPhys** — better motion robustness, requires GPU inference.

**POS Algorithm:** (a) temporally normalize each channel by dividing by running mean, (b) project onto chrominance axes S1/S2, (c) combine using adaptive ratio based on standard deviations to cancel motion-correlated components, (d) apply overlap-add windowing for continuous BVP signal.

| Parameter | Specification |
|-----------|---------------|
| ROI Selection | MediaPipe FaceMesh landmarks: forehead (10, 67, 69, 104, 108, 151, 299, 337, 338), left cheek (50, 101, 116–121), right cheek (mirrored) |
| Signal Window | 10-second sliding window (300 frames) with 1-second stride |
| Bandpass Filter | Butterworth 4th-order, 0.7–3.5 Hz (42–210 BPM range) |
| HR Estimation | Welch PSD with FFT resolution of 0.1 Hz; peak detection in filtered spectrum |
| HRV Metric | RMSSD from inter-beat interval series derived from BVP peak detection |
| Output Rate | HR updated every 1 second; HRV updated every 5 seconds (requires longer window) |

**Function API:**
```python
extract_rgb_traces(frame: np.ndarray, landmarks: FaceLandmarks) -> RoiTraceFrame
update_rppg_buffer(trace_frame: RoiTraceFrame) -> None
compute_pulse_window(buffer_window: np.ndarray) -> PulseFeatures
score_signal_quality(buffer_window: np.ndarray, pulse_features: PulseFeatures) -> SignalQuality
```

**Known Limitations and Mitigations:**
| Limitation | Mitigation |
|------------|------------|
| Ambient lighting sensitivity (< 50 lux) | Fall back to telemetry-only heuristics; prompt for lighting improvement |
| Skin tone bias (lower SNR on darker tones) | Use CHROM as fallback; per-user calibration during onboarding |
| Motion artifacts (large head movements) | MediaPipe landmark stability as gating; discard frames exceeding jitter threshold |
| Eyeglasses / facial hair ROI occlusion | Dynamically select from multiple ROI candidates; use highest-SNR region |

#### kinematics_engine

**Blink Rate Detection:**
Average resting blink rate: 15–20/min. Drops during high cognitive load (< 8/min = blink suppression). MediaPipe FaceMesh Eye Aspect Ratio (EAR) = ratio of vertical eye opening to horizontal width. Blink detected when EAR drops below 0.21 for ≥ 3 consecutive frames (100ms at 30fps) and recovers above 0.25.

**Shoulder Posture Tracking:**
MediaPipe Pose landmarks 11/12 (shoulders). Two metrics:
- Shoulder drop ratio: vertical displacement of shoulder midpoint vs. calibrated neutral
- Forward lean angle: angle between shoulder-ear line and vertical
- Posture collapse: shoulder drop > 15% from baseline + forward lean > 20°

**Output:**
```python
@dataclass
class KinematicFeatures:
    blink_rate: float | None          # blinks/min
    blink_suppression_score: float | None
    head_pitch: float | None          # degrees
    head_yaw: float | None
    head_roll: float | None
    slump_score: float | None         # 0-1 normalized
    forward_lean_score: float | None  # 0-1 normalized
    shoulder_drop_ratio: float | None
    confidence: float
```

#### telemetry_engine

**Raw Inputs:**
- Mouse position, clicks, scroll events (via pynput, sampled at 60Hz → downsampled to 10Hz)
- Keyboard activity (inter-keystroke intervals)
- Active window changes / app focus (via OS-specific hooks)
- Browser tab count (if extension available)
- Editor file changes (if extension available)

**Derived Features:**
| Feature | Description |
|---------|-------------|
| mouse_velocity_mean | Mean mouse speed (px/s) over window |
| mouse_jerk_score | Acceleration variance — erratic movement |
| click_burst_score | Repeated rapid clicks |
| keyboard_burst_score | Typing intensity spikes |
| backspace_density | Deletion-to-keystroke ratio |
| inactivity_seconds | Time since last input |
| window_switch_rate | App/window switches per minute |
| tab_count | Number of open browser tabs |
| scroll_reversal_score | Repeated scroll direction changes |
| keystroke_interval_variance | Typing rhythm regularity (ms²) |

**Platform Abstraction:**
- macOS: pynput + pyobjc for window info
- Linux: pynput + python-xlib / ewmh
- Windows: pynput + ctypes / pywin32

**Function API:**
```python
record_mouse_event(event: MouseEvent) -> None
record_key_event(event: KeyEvent) -> None
record_window_focus(event: WindowFocusEvent) -> None
build_telemetry_features(window_seconds: float = 15.0) -> TelemetryFeatures
```

#### Unified Feature Vector (12-dimensional, every 500ms)

| # | Feature | Source | Range / Unit |
|---|---------|--------|-------------|
| 1 | Instantaneous Heart Rate | rPPG | 40–200 BPM |
| 2 | RMSSD (HRV proxy) | rPPG | 10–200 ms |
| 3 | HR Delta (5s gradient) | rPPG | -30 to +30 BPM/5s |
| 4 | Blink Rate | FaceMesh | 0–40 blinks/min |
| 5 | Blink Rate Delta | FaceMesh | Change from 60s baseline |
| 6 | Shoulder Drop Ratio | Pose | 0–1.0 normalized |
| 7 | Forward Lean Angle | Pose | 0–45 degrees |
| 8 | Mouse Velocity (mean) | pynput | 0–5000 px/s |
| 9 | Mouse Velocity (variance) | pynput | Statistical dispersion |
| 10 | Click Frequency | pynput | 0–20 clicks/s |
| 11 | Keystroke Interval Variance | pynput | ms² |
| 12 | Tab Switch Frequency | Extension | 0–60 switches/min |

### L2: State Classification Engine

**Target State Taxonomy (Polyvagal Theory):**

| Zone | Physiological Signature | Behavioral Signature |
|------|------------------------|---------------------|
| **FLOW** (Optimal) | HR within 10% of baseline, HRV elevated (RMSSD > 40ms), blink rate 12–20/min | Steady typing, focused tabs, low mouse variance, upright posture |
| **HYPO** (Under-arousal) | HR below baseline, HRV dropping, blink rate elevated (> 25/min) | Mouse drift, long pauses, posture slump, minimal tab switching |
| **HYPER** (Over-arousal) | HR spike > 15% above baseline, HRV crash (RMSSD < 20ms), blink suppression (< 8/min) | Erratic mouse, rapid tab thrashing (> 20/min), forward lean > 20° |
| **RECOVERY** | Transitioning from HYPER/HYPO back toward FLOW | Mixed signals, declining overwhelm indicators |

**V1: Rule-Based Score Engine**

Hyper-Arousal Score:
```
hyper_score =
    w1 * pulse_elevation_score          # HR > baseline + 15%
  + w2 * pulse_variability_drop_score   # RMSSD < 20ms
  + w3 * blink_suppression_score        # blink rate < 8/min
  + w4 * posture_collapse_score         # forward lean > 20°
  + w5 * mouse_thrashing_score          # velocity variance > 3x baseline
  + w6 * window_switch_score            # > 20 switches/min
  + w7 * workspace_complexity_score     # error density + tab count
```
Default weights: w1=0.20, w2=0.15, w3=0.12, w4=0.08, w5=0.15, w6=0.15, w7=0.15

**State Logic:**
- Compute per-state score, normalize
- Smooth over rolling history (exponential moving average, α=0.3)
- Dwell time before state transition (HYPER: 8s, HYPO: 15s)
- Hysteresis thresholds (entry: 0.85, exit: 0.70)
- Return StateEstimate with state, confidence, reasons, signal quality per channel

**Intervention Hysteresis:**
| Condition | Threshold |
|-----------|-----------|
| Intervention trigger | HYPER confidence > 0.85 sustained for > 8 seconds |
| Intervention withdrawal | FLOW confidence > 0.70 sustained for > 15 seconds |
| Cooldown after withdrawal | Minimum 60 seconds before re-triggering |
| User override | Escape key or click dismisses. 3 consecutive dismissals within 5 min → 30-min quiet mode |
| Adaptive learning | Each dismissal raises trigger threshold by 0.05 for next hour |

**V2 Path: Temporal Convolutional Network (TCN)**
| Component | Specification |
|-----------|---------------|
| Input Shape | (batch, 128, 12) — 128 timesteps × 12 features at 2Hz = 64 seconds |
| TCN Blocks | 4 residual blocks, dilation [1, 2, 4, 8], 64 filters, kernel size 3, causal padding, dropout 0.2 |
| Global Pooling | Temporal average pooling |
| Classification Head | Dense(64, ReLU) → Dropout(0.3) → Dense(4, Softmax) |
| Total Parameters | ~85K |
| Inference Latency | < 5ms on modern CPU |
| Framework | PyTorch with ONNX export via onnxruntime |

### L3: Context Engine — Workspace Adapters

#### VS Code Adapter (TypeScript Extension)

**Required Extension Commands:**
| Command | Description |
|---------|-------------|
| cortex.getActiveFile | Current file path + visible range |
| cortex.getDiagnostics | All errors/warnings for current file |
| cortex.getSymbolAtCursor | Current function/class/symbol |
| cortex.foldExcept | Fold everything except specified range |
| cortex.unfoldAll | Restore all folds |
| cortex.showPanel | Show Cortex intervention side panel |
| cortex.restoreFoldState | Restore pre-intervention fold state |

**Best Intervention:** Fold everything except current function/block, show 1–3 steps in side panel, highlight suspicious lines.

#### Browser Adapter (Chrome Extension — Manifest V3, Plasmo)

**Extension Components:**
| Component | Responsibility |
|-----------|---------------|
| Service Worker (background.js) | Maintains WebSocket to Cortex daemon (ws://localhost:9473). Receives commands. Dispatches content script injection. |
| Content Script (cortex-inject.js) | Injected into active tab on trigger. Captures DOM text via TreeWalker. Executes DOM manipulation: overlay, folding, dimming. Shadow DOM encapsulation. |
| Popup UI | Status dashboard: state indicator, HR estimate, sensitivity toggles, quiet mode. |

**Required Features:**
- Active-tab content extraction (visible text, limited to 2000 tokens)
- Tab title/URL collection for all open tabs
- Complexity score heuristics
- Focus mode overlay
- Temporary hide/group tabs + restore

**Best Intervention:** Hide nonessential tabs, summarize current page and relevant tabs, display single next action.

#### Terminal Adapter (V1: Log Parsing)

- Shell wrapper or VS Code terminal API capture
- Capture recent N lines
- Detect error blocks (stack traces)
- Condense error output
- Identify likely root-cause region

#### Context Output Contract

```python
@dataclass
class TaskContext:
    mode: str                          # "coding_debugging" | "reading_docs" | "browsing" | "terminal_errors"
    active_app: str                    # "vscode" | "chrome" | "terminal"
    current_goal_hint: str | None
    complexity_score: float            # 0-1
    editor_context: EditorContext | None
    terminal_context: TerminalContext | None
    browser_context: BrowserContext | None

@dataclass
class EditorContext:
    file_path: str
    visible_range: tuple[int, int]
    symbol_at_cursor: str | None
    diagnostics: list[Diagnostic]
    recent_edits: list[str]
    visible_code: str

@dataclass
class TerminalContext:
    last_n_lines: list[str]
    detected_errors: list[str]
    repeated_commands: list[str]
    running_command: str | None

@dataclass
class BrowserContext:
    active_tab_title: str
    active_tab_url: str
    active_tab_content_excerpt: str    # ≤ 2000 tokens
    all_tab_titles: list[str]
    tab_count: int
    tab_type_classification: dict[str, int]
```

### L4: LLM Scaffolding Engine

**Required Capabilities:**
- Summarize code/debugging state
- Identify the single immediate bottleneck
- Suppress irrelevant details
- Return a micro-step plan (1–3 steps)
- Produce content folding instructions
- Produce calming but direct intervention text

**LLM Output Schema:**
```json
{
  "situation_summary": "string, 1-2 sentences",
  "primary_focus": "string, the one thing to look at",
  "headline": "string, under 15 words",
  "micro_steps": ["step 1", "step 2", "step 3"],
  "hide_targets": [
    "browser_tabs_except_active",
    "terminal_lines_before_last_error_block",
    "editor_symbols_except_current_function"
  ],
  "ui_plan": {
    "dim_background": true,
    "show_overlay": true,
    "fold_unrelated_code": true,
    "intervention_type": "simplified_workspace"
  },
  "tone": "direct"
}
```

**Engineering Constraints:**
- Model ONLY returns a plan — never directly executes actions
- Intervention engine validates and applies the plan
- Max latency: < 3s time-to-first-token, < 8s total generation
- Use caching for repeated similar contexts
- Fault-tolerant JSON parsing (json-repair or manual fixer)
- Validate parsed payload against Pydantic schema before dispatch
- If parsing fails after 2 retries → fall back to generic rule-based intervention

**Prompt Types:**
| Prompt | Purpose |
|--------|---------|
| debug_error_summary | Condense terminal error flood into root cause + 1 action |
| code_focus_reduction | Identify which code region matters and what to fold |
| browser_tab_reduction | Triage tabs by relevance to current task |
| micro_step_planner | Generate 1-3 concrete next steps |
| calm_overlay_writer | Produce empathetic, non-patronizing intervention text |

**System Prompt Template:**
```
You are Cortex, a calm and direct workspace assistant. The user is experiencing
cognitive overwhelm while coding/debugging. Your job is to analyze their current
workspace context and produce a structured intervention plan.

Rules:
- Only use the provided context. Do not hallucinate file names, line numbers, or errors.
- Identify the ONE immediate bottleneck.
- Suppress irrelevant details.
- Return 1-3 concrete micro-steps (not generic advice like "take a break").
- Specify which workspace elements to hide/fold.
- Keep the headline under 15 words.
- Never recommend destructive actions (deleting files, closing unsaved buffers).
- Output ONLY valid JSON matching the schema below. No markdown, no preamble.
```

**Context Injection Order:**
1. Viewport snapshot (visible code/text, ≤ 2000 tokens)
2. Tab context (titles and URLs, sorted by recent active time)
3. Physiological context (natural-language state summary)
4. Terminal context (last error block)
5. Editor context (current file, function, diagnostics)

**Best Use of Qwen-3-8B:**
- USE for: code/log comprehension, doc summarization, identifying minimum next action, folding/simplification directives
- DO NOT use for: raw state classification, pixel-level perception, free-form agentic exploration

### L5: Intervention Engine & UI Interceptor

**Trigger Conditions (ALL must be true):**
```python
if (state == "HYPER"
    and confidence > 0.85
    and workspace_complexity > 0.6
    and signal_quality_acceptable
    and cooldown_inactive
    and no_recent_dismissal):
    trigger_intervention()
```

**Intervention Types:**

| Type | Description |
|------|-------------|
| **A — Overlay Only** (Mildest) | Summary + one next step. No workspace changes. Semi-transparent overlay (soft blues/whites, 18px min type). Optional breathing pacer (4-7-8 pattern). Triggered at lower confidence (> 0.70). |
| **B — Simplified Workspace** | Dim inactive windows (rgba(0,0,0,0.7)). Fold unrelated code. Collapse terminal lines. Hide extra browser tabs. One-line LLM summaries for collapsed sections. |
| **C — Guided Mode** (Most Aggressive) | Replace screen region with focused checklist. Suppress noisy UI elements. Background tabs accessible but visually recessed. Restoration token required to exit. |

**Restore Logic:**
1. Before any mutation: save restoration snapshot (fold state, tab visibility, overlay presence)
2. On recovery or dismissal: restore tab visibility, code folds, terminal view, remove overlay
3. Every mutation reversible via explicit cancel button
4. Interventions timeout safely (max 5 min, then auto-restore)
5. CANNOT delete files / tabs / buffers — only visual changes

**Safeguards:**
- Every mutation reversible
- Explicit cancel button (Escape key or click)
- Interventions timeout safely
- Cannot delete files, close tabs, or modify buffers
- User dismissal increases cooldown
- 3 consecutive dismissals → 30-min quiet mode

### Desktop Shell & Control Surface

**V1: PySide6 overlay/control panel + FastAPI backend**

**UI Components:**
| Component | Description |
|-----------|-------------|
| Tray icon | Always-visible status indicator |
| Live state indicator | Current state (FLOW/HYPO/HYPER/RECOVERY) + confidence |
| Webcam toggle | Enable/disable webcam sensing |
| Intervention toggle | Enable/disable auto-interventions |
| Sensitivity slider | Adjust trigger thresholds (1–5 scale) |
| Debug dashboard | Live HR trace, HRV, blink rate, posture angles, mouse velocity, telemetry |
| Session timeline | Historical state transitions + intervention events |
| Intervention modal | Rendered overlay with LLM-generated content |

**Minimum UI for V1:**
- Simple control panel window
- Intervention overlay window (transparent, always-on-top)
- Manual pause/resume
- Current state + confidence + last trigger reason
- Settings: sensitivity, cooldown duration, quiet mode

## Data Model

### Core Schemas (Pydantic)

```python
class FrameMeta(BaseModel):
    timestamp: float
    face_detected: bool
    face_confidence: float
    brightness_score: float
    blur_score: float
    motion_score: float

class PhysioFeatures(BaseModel):
    pulse_bpm: float | None
    pulse_quality: float               # 0-1, SNR-like
    pulse_variability_proxy: float | None  # RMSSD
    hr_delta_5s: float | None
    valid: bool

class KinematicFeatures(BaseModel):
    blink_rate: float | None
    blink_rate_delta: float | None
    blink_suppression_score: float | None
    head_pitch: float | None
    head_yaw: float | None
    head_roll: float | None
    slump_score: float | None
    forward_lean_score: float | None
    shoulder_drop_ratio: float | None
    confidence: float

class TelemetryFeatures(BaseModel):
    mouse_velocity_mean: float
    mouse_velocity_variance: float
    mouse_jerk_score: float
    click_burst_score: float
    click_frequency: float
    keyboard_burst_score: float
    keystroke_interval_variance: float
    backspace_density: float
    inactivity_seconds: float
    window_switch_rate: float
    tab_count: int | None
    scroll_reversal_score: float | None

class FeatureVector(BaseModel):
    timestamp: float
    hr: float | None
    hrv_rmssd: float | None
    hr_delta: float | None
    blink_rate: float | None
    blink_rate_delta: float | None
    shoulder_drop_ratio: float | None
    forward_lean_angle: float | None
    mouse_velocity_mean: float
    mouse_velocity_variance: float
    click_frequency: float
    keystroke_interval_variance: float
    tab_switch_frequency: float

class StateEstimate(BaseModel):
    state: str                          # "FLOW" | "HYPO" | "HYPER" | "RECOVERY"
    confidence: float
    scores: dict[str, float]
    reasons: list[str]
    signal_quality: dict[str, float]

class TaskContext(BaseModel):
    mode: str
    active_app: str
    complexity_score: float
    goal_hint: str | None
    editor_context: dict | None
    terminal_context: dict | None
    browser_context: dict | None

class InterventionPlan(BaseModel):
    level: str                          # "overlay_only" | "simplified_workspace" | "guided_mode"
    situation_summary: str
    headline: str                       # < 15 words
    primary_focus: str
    micro_steps: list[str]              # 1-3 items
    hide_targets: list[str]
    ui_plan: dict
    intervention_id: str
```

## API / Interface

### Internal Service APIs (FastAPI)

| Endpoint | Method | Description |
|----------|--------|-------------|
| /capture/frame_meta | POST | Submit frame metadata |
| /features/physio | POST | Submit physio features |
| /features/kinematics | POST | Submit kinematic features |
| /features/telemetry | POST | Submit telemetry features |
| /state/infer | POST | Compute state from fused features |
| /context/build | POST | Build task context from adapters |
| /llm/plan | POST | Request intervention plan from LLM |
| /intervention/apply | POST | Apply intervention to workspace |
| /intervention/restore | POST | Restore workspace to pre-intervention state |
| /status/current | GET | Current system state, confidence, signal quality |
| /health | GET | Health check for all services |

### LLM Server API (on gwhiz1)

```
POST /v1/chat/completions          # OpenAI-compatible
POST /generate_intervention_plan    # Convenience endpoint
```

### WebSocket Protocol (Daemon ↔ Extensions)

JSON-over-WebSocket on ws://localhost:9473. Three message types:

| Message | Direction | Description |
|---------|-----------|-------------|
| STATE_UPDATE | daemon → extension | Every 500ms. State, confidence, features, sequence number. |
| INTERVENTION_TRIGGER | daemon → extension | Intervention type, LLM payload, unique intervention ID. |
| USER_ACTION | extension → daemon | dismissed, engaged, snoozed. Includes intervention ID + timestamp. |

## Repository Structure

```
cortex/
├── apps/
│   ├── desktop_shell/                  # PySide6 control panel + overlay
│   │   ├── main.py
│   │   ├── tray.py
│   │   ├── overlay.py
│   │   ├── dashboard.py
│   │   └── settings.py
│   ├── browser_extension/              # Manifest V3 Chrome/Edge extension
│   │   ├── manifest.json
│   │   ├── background.ts
│   │   ├── content.tsx
│   │   ├── popup.tsx
│   │   └── options.tsx
│   └── vscode_extension/              # VS Code extension
│       ├── package.json
│       ├── src/
│       │   ├── extension.ts
│       │   ├── context-provider.ts
│       │   ├── fold-controller.ts
│       │   ├── panel-provider.ts
│       │   └── ws-client.ts
│       └── tsconfig.json
├── services/
│   ├── api_gateway/
│   │   ├── __init__.py
│   │   ├── app.py
│   │   ├── routes.py
│   │   └── websocket_server.py
│   ├── capture_service/
│   │   ├── __init__.py
│   │   ├── webcam.py
│   │   ├── face_tracker.py
│   │   └── quality.py
│   ├── physio_engine/
│   │   ├── __init__.py
│   │   ├── roi_extractor.py
│   │   ├── rppg.py
│   │   ├── pulse_estimator.py
│   │   └── quality_scorer.py
│   ├── kinematics_engine/
│   │   ├── __init__.py
│   │   ├── blink_detector.py
│   │   ├── head_pose.py
│   │   └── posture.py
│   ├── telemetry_engine/
│   │   ├── __init__.py
│   │   ├── input_hooks.py
│   │   ├── window_tracker.py
│   │   └── feature_aggregator.py
│   ├── state_engine/
│   │   ├── __init__.py
│   │   ├── feature_fusion.py
│   │   ├── rule_scorer.py
│   │   ├── smoother.py
│   │   └── trigger_policy.py
│   ├── context_engine/
│   │   ├── __init__.py
│   │   ├── app_classifier.py
│   │   ├── editor_adapter.py
│   │   ├── browser_adapter.py
│   │   └── terminal_adapter.py
│   ├── llm_engine/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── remote_qwen.py
│   │   ├── local_ollama.py
│   │   ├── prompts.py
│   │   ├── parser.py
│   │   └── cache.py
│   └── intervention_engine/
│       ├── __init__.py
│       ├── trigger.py
│       ├── planner.py
│       ├── executor.py
│       ├── snapshot.py
│       └── restore.py
├── libs/
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── features.py
│   │   ├── state.py
│   │   ├── context.py
│   │   └── intervention.py
│   ├── config/
│   │   ├── __init__.py
│   │   ├── settings.py
│   │   └── defaults.yaml
│   ├── logging/
│   │   ├── __init__.py
│   │   └── structured.py
│   ├── signal/
│   │   ├── __init__.py
│   │   ├── filters.py
│   │   ├── peak_detection.py
│   │   └── windowing.py
│   └── utils/
│       ├── __init__.py
│       ├── async_helpers.py
│       └── platform.py
├── storage/
│   ├── sessions/
│   └── cache/
├── scripts/
│   ├── run_dev.py
│   ├── run_capture.py
│   ├── run_llm_server.py
│   ├── setup_ssh_tunnel.sh
│   ├── replay_session.py
│   ├── seed_config.py
│   └── calibrate.py
├── tests/
│   ├── unit/
│   │   ├── test_roi_extraction.py
│   │   ├── test_filters.py
│   │   ├── test_pulse_window.py
│   │   ├── test_blink_detection.py
│   │   ├── test_telemetry_features.py
│   │   ├── test_state_scoring.py
│   │   └── test_json_parsing.py
│   ├── integration/
│   │   ├── test_capture_to_state.py
│   │   ├── test_context_to_llm.py
│   │   ├── test_intervention_cycle.py
│   │   ├── test_vscode_fold_restore.py
│   │   └── test_browser_hide_restore.py
│   └── fixtures/
│       ├── sample_features.json
│       ├── sample_context.json
│       └── sample_llm_response.json
├── docs/
│   ├── architecture.md
│   ├── apis.md
│   ├── setup.md
│   ├── calibration.md
│   └── adapters.md
├── pyproject.toml
├── README.md
└── .env.example
```

## Core Algorithms

### Overwhelm Score (Fused)

```python
def compute_overwhelm_score(features: FeatureVector, baselines: UserBaselines) -> float:
    scores = {
        "pulse_elevation":      score_pulse_elevation(features.hr, baselines.hr_baseline),
        "hrv_drop":             score_hrv_drop(features.hrv_rmssd, baselines.hrv_baseline),
        "blink_suppression":    score_blink_suppression(features.blink_rate),
        "posture_collapse":     score_posture(features.forward_lean_angle, features.shoulder_drop_ratio),
        "mouse_thrashing":      score_mouse_thrash(features.mouse_velocity_variance, baselines.mouse_var_baseline),
        "window_switching":     score_window_switch(features.tab_switch_frequency),
        "workspace_complexity": score_workspace_complexity(context),
    }
    weights = config.overwhelm_weights
    return sum(weights[k] * scores[k] for k in scores)
```

### Workspace Complexity Score

Fuse: terminal error density, line count / visible clutter, number of open browser tabs, window switch rate, size of visible code block, diagnostic count, document density.

### Trigger Policy

```python
async def check_intervention(state: StateEstimate, context: TaskContext, policy: TriggerPolicy):
    if state.state != "HYPER": return None
    if state.confidence < policy.confidence_threshold: return None       # default 0.85
    if context.complexity_score < policy.complexity_threshold: return None # default 0.6
    if not signal_quality_acceptable(state.signal_quality): return None
    if policy.cooldown_active: return None
    if policy.recently_dismissed(window_minutes=5, max_dismissals=3): return None
    if not policy.dwell_check(state, duration_seconds=8): return None
    plan = await llm_engine.generate_intervention_plan(context, state)
    return plan
```

## Privacy Architecture

| Principle | Implementation |
|-----------|----------------|
| No video recording | Frames processed in-memory; raw pixels discarded immediately after ROI RGB extraction |
| No face reconstruction | rPPG operates on spatial averages of 60×40px region — mathematically irreversible |
| No data egress | All sensing local. LLM calls send only text context, never video or biometric data |
| Minimal permissions | Chrome: activeTab + scripting only. No browsing history access. |
| Local storage only | Scalar time-series, aggregated statistics, config. No raw biometric storage. |
| Informed consent | Never activates without explicit opt-in. Onboarding explains data handling. |
| Transparency | Popup always shows current state. Full intervention log accessible. |

## Logging, Debugging & Replay

### Logging Strategy

All events logged as structured JSON via Python `structlog`.

| Event Class | Example Fields | Retention |
|-------------|---------------|-----------|
| State transition | timestamp, old_state, new_state, confidence, reasons | 30 days |
| Intervention triggered | intervention_id, level, trigger_reasons, context_summary | 30 days |
| Intervention outcome | intervention_id, user_action, duration_seconds | 30 days |
| Feature vector | timestamp, 12 features (every 500ms) | 7 days (or session) |
| Error/exception | timestamp, service, message, traceback | 90 days |
| LLM request/response | prompt_hash, response_hash, latency_ms, token_count | 7 days |

### Session Replay

- Sessions stored as JSONL (one object per line)
- Replay tool (scripts/replay_session.py) loads JSONL and replays state transitions
- Dashboard can visualize historical sessions

### Debug Modes

- `--debug-capture`: Save annotated frames to disk (face mesh overlay)
- `--debug-rppg`: Save raw RGB traces + BVP signal to CSV
- `--debug-state`: Print feature vectors and scores to stdout
- `--debug-llm`: Save full prompt/response pairs

## Behavior

### Core Requirements

1. Continuously monitor user physiological state via webcam at 30fps without saving video
2. Extract heart rate, HRV, blink rate, posture, and head pose from webcam feed
3. Capture mouse, keyboard, and window-switching telemetry
4. Fuse all signals into 12-dimensional feature vector every 500ms
5. Classify user state into FLOW, HYPO, HYPER, or RECOVERY with confidence scores
6. When HYPER state detected with sufficient confidence and workspace complexity, gather workspace context
7. Send context to Qwen-3-8B on gwhiz1 to generate structured intervention plan
8. Apply intervention: fold code, hide tabs, show overlay with micro-steps
9. Monitor for recovery; restore workspace when user returns to FLOW
10. All interventions must be fully reversible — no destructive actions
11. Respect user dismissals with escalating cooldowns
12. Fall back to rule-based interventions when LLM is unavailable
13. All biometric data stays local — only text context sent to LLM
14. Support VS Code, Chrome, and terminal as workspace targets

### Edge Cases

- Webcam unavailable or blocked → fall back to telemetry-only mode
- Poor lighting (< 50 lux) → disable rPPG, use telemetry + kinematics only
- Face not detected for > 30s → fall back to telemetry-only mode
- LLM server unreachable → use rule-based intervention (fold all except current function, generic overlay)
- LLM returns invalid JSON → retry once, then fall back to rule-based
- User rapidly dismisses interventions → escalating quiet mode (3 dismissals → 30 min quiet)
- Multiple monitors → track active monitor only
- Screen sharing detected → disable overlay interventions
- VS Code not running → skip editor adapter, use browser + terminal only
- No browser extension installed → skip browser adapter

### Out of Scope (V1)

- Wearable device integration (Apple Watch, Fitbit)
- Multi-user / team monitoring
- Cloud deployment or SaaS
- Mobile platform support
- EEG or GSR sensor integration
- Automated code fixes (Cortex only suggests, never modifies code)
- Support for IDEs other than VS Code
- Browsers other than Chromium-based

## Failure Modes & Required Fallbacks

| Failure | Detection | Fallback |
|---------|-----------|----------|
| Webcam unavailable | cv2.VideoCapture fails or returns empty frames | Telemetry-only mode (reduced confidence thresholds) |
| Poor lighting | brightness_score < 0.3 consistently | Disable rPPG; rely on kinematics + telemetry |
| Face tracking lost | face_detected = False for > 30 consecutive frames | Log gap; resume on reacquire; use telemetry meanwhile |
| rPPG signal too noisy | pulse_quality < 0.3 consistently | Switch to CHROM; if still poor, disable physio channel |
| LLM server unreachable | HTTP timeout or connection refused | Rule-based intervention only |
| LLM returns garbage | JSON parse failure after 2 retries | Generic intervention (fold all except current, show calming overlay) |
| Extension not installed | WebSocket connection refused | Skip that adapter; use remaining adapters |
| Input hooks fail | pynput throws PermissionError | Log warning; disable telemetry channel; adjust confidence weighting |
| SQLite corruption | sqlite3.DatabaseError | Recreate DB from defaults; log incident |
| Memory pressure | RSS > threshold | Drop frame processing rate; disable debug logging |

## Testing Plan

### Unit Tests
- ROI extraction from landmarks (test_roi_extraction.py)
- Butterworth bandpass filter (test_filters.py)
- POS algorithm on synthetic PPG signal (test_pulse_window.py)
- Blink detection from EAR sequences (test_blink_detection.py)
- Telemetry feature aggregation (test_telemetry_features.py)
- State scoring and hysteresis (test_state_scoring.py)
- LLM JSON parsing with malformed inputs (test_json_parsing.py)

### Integration Tests
- Full capture → face → rPPG → features → state pipeline (test_capture_to_state.py)
- Context gathering → LLM call → intervention plan (test_context_to_llm.py)
- Full intervention lifecycle: trigger → apply → restore (test_intervention_cycle.py)
- VS Code fold/restore round-trip (test_vscode_fold_restore.py)
- Browser tab hide/restore round-trip (test_browser_hide_restore.py)

### Test Fixtures
- sample_features.json: Pre-computed feature vectors for various states
- sample_context.json: Example workspace context objects
- sample_llm_response.json: Example LLM intervention plans (valid + malformed)

## Acceptance Criteria

- [ ] Webcam captures at 30fps and extracts face landmarks with < 50ms latency per frame
- [ ] rPPG produces heart rate estimate within ±5 BPM of ground truth on UBFC-rPPG dataset
- [ ] HRV (RMSSD) computed from IBI series with 5-second update interval
- [ ] Blink detection achieves > 90% accuracy against manual annotation
- [ ] Telemetry captures mouse, keyboard, and window events with < 10ms latency
- [ ] 12-dim feature vector produced every 500ms ± 50ms
- [ ] State classification into FLOW/HYPO/HYPER/RECOVERY with confidence scores
- [ ] State transitions respect hysteresis (8s dwell for HYPER, 15s for HYPO)
- [ ] VS Code extension folds/unfolds code and shows intervention panel
- [ ] Chrome extension captures tab context and applies focus mode overlay
- [ ] LLM client successfully calls Qwen-3-8B on gwhiz1 and receives structured JSON
- [ ] Intervention plan validated against Pydantic schema before application
- [ ] Full intervention cycle (detect → gather context → LLM → apply) completes in < 12s
- [ ] Workspace fully restored on recovery or user dismissal
- [ ] No raw video or biometric data leaves local machine
- [ ] System falls back gracefully when webcam/LLM/extensions unavailable
- [ ] All unit tests pass with > 85% coverage
- [ ] Integration tests cover capture→state, context→LLM, and intervention lifecycle
- [ ] Desktop control panel shows live state, allows pause/resume, and sensitivity adjustment

## Examples

### Example 1: Coding Overwhelm Detection

**Input:**
- HR: 108 BPM (baseline: 72), HRV RMSSD: 15ms, blink rate: 4/min
- Mouse velocity variance: 4x baseline, tab switches: 25/min
- VS Code: 3 error diagnostics, 200-line file visible, cursor in function `parse_config`
- Terminal: Python traceback (KeyError in line 47)
- Browser: 12 tabs open (5 StackOverflow, 3 docs, 4 other)

**Expected Output:**
- State: HYPER (confidence: 0.92)
- Intervention: Simplified Workspace
- LLM plan: Focus on KeyError in parse_config line 47, fold everything except lines 40-60, hide 9 of 12 browser tabs, show 2 micro-steps

### Example 2: Recovery and Restore

**Input:**
- Active intervention (Simplified Workspace) applied 3 minutes ago
- HR now: 78 BPM, HRV RMSSD: 45ms, blink rate: 16/min
- Steady typing, no tab switching
- State: FLOW (confidence: 0.75, sustained for 20s)

**Expected Output:**
- Withdrawal condition met (FLOW > 0.70 for > 15s)
- Restore: unfold code, show all tabs, remove overlay
- Log intervention outcome: duration=180s, user_action=natural_recovery

### Example 3: LLM Fallback

**Input:**
- HYPER detected, context gathered
- LLM server on gwhiz1 unreachable (SSH tunnel down)

**Expected Output:**
- Fall back to rule-based intervention
- Fold all code except current function
- Show generic overlay: "Take it one step at a time. Focus on the function you're in."
- Log: llm_fallback_triggered, reason=connection_refused

## Notes

- **Performance targets:** sub-200ms signal-to-state latency; sub-12s overwhelm-to-intervention
- **Remote GPU:** ssh wangcy07@gwhiz1.cis.upenn.edu — Qwen-3-8B already deployed
- **SSH tunnel for LLM:** ssh -L 8800:localhost:<PORT> wangcy07@gwhiz1.cis.upenn.edu
- **No wearables required** — webcam only
- **macOS primary target** — Linux secondary, Windows tertiary
- **V2 upgrade paths:** EfficientPhys for rPPG, TCN for state classification, Tauri for desktop shell
- **IRB required** for user study data collection (V2 TCN training)
