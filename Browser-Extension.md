# Browser Extension

The Cortex browser extension runs in Chrome and Edge (Manifest V3, built with Plasmo/React). It handles daemon launch/stop, displays the intervention overlay, provides ambient somatic feedback, tracks activity, and shows the Pulse Room new tab.

---

## Installation

### Build

```bash
cd cortex/apps/browser_extension
pnpm install

# Chrome
npx plasmo build

# Edge
npx plasmo build --target=edge-mv3

# Development with hot reload
pnpm dev
```

### Load in Browser

**Chrome:** `chrome://extensions` → Enable Developer mode → Load unpacked → `build/chrome-mv3-prod/`

**Edge:** `edge://extensions` → Enable Developer mode → Load unpacked → `build/edge-mv3-prod/`

---

## Native Messaging Setup

Native messaging lets the extension start and stop the daemon without using the terminal.

```bash
# From the repo root
python -m cortex.scripts.install_native_host
```

Then **fully restart your browser** (Cmd+Q, reopen). Reloading the extension is not enough — Chrome only reads native messaging manifests at startup.

First use: macOS will ask *"Chrome/Edge wants to control Terminal. Allow?"* — click **Allow** once.

---

## Popup

Click the Cortex icon in the browser toolbar.

| Button | Action |
|--------|--------|
| **Start Cortex** | Launches daemon via Terminal.app (required for camera TCC access) |
| **Stop Cortex** | Sends multi-layer kill chain: WebSocket → HTTP → process kill |
| **Restart Camera** | Stops and restarts the daemon (useful after turning off iPhone) |

The popup shows current cognitive state, signal quality, and camera status when the daemon is running.

---

## Intervention Overlay

When Cortex detects HYPER, the intervention overlay appears in the bottom-right of your active tab:

- **Headline** — ≤15-word focus instruction from the LLM
- **Situation summary** — why it triggered, referencing observable behavior
- **Micro-steps** — 1-3 concrete actions
- **Tab recommendations** — keep/close/group suggestions per open tab
- **Error analysis** — root cause and suggested fix when errors are detected

Overlay controls:
- **Execute actions** — apply suggested tab changes with one click (preview mode shows what will change before confirming)
- **Dismiss** — restore workspace to pre-intervention state
- **Snooze** — quiet mode for 15/30/60 minutes
- **Rate** — thumbs up/down to teach the bandit what helps you

---

## Pulse Room (New Tab)

The new tab page is the Pulse Room — a bio-responsive ambient environment:

- **Ambient particles** — count and speed respond to rPPG pulse rate
- **Color vignette** — sub-threshold color shift based on cognitive state (invisible during FLOW, warm during HYPER)
- **Flow shield** — fades distraction elements during sustained focus sessions
- **Resume cards** — one-click cards to return to interrupted learning sessions (YouTube, Coursera, LeetCode, etc.)

---

## Focus Sessions

Start a focus session from the popup or new tab:

- Distraction blocking — auto-close tabs matching distraction patterns
- Session timer with biology-driven extension (the session extends while you're in FLOW)
- End-of-session summary showing sustained focus time and interventions received

---

## LeetCode Observer

When a LeetCode problem tab is active, the extension injects a DOM observer that:

- Detects current problem-solving stage (READ / PLAN / IMPLEMENT / DEBUG / REFLECT)
- Shows stage-appropriate hints from the pattern ladder
- Detects panic-coding patterns and activates the amygdala hijack lockout
- Guards against low-quality submissions (no test coverage, rapid re-submit)

---

## Activity Tracking

The extension tracks learning progress across:

| Platform | What's tracked |
|----------|---------------|
| YouTube / Bilibili | Video timestamp, title, channel |
| Coursera | Module, lesson, completion % |
| LeetCode | Problem name, attempt count, accepted/rejected |
| PDFs | Scroll position, page estimate |
| Jupyter Notebooks | Cell index |
| GitHub | Repo + file |

Resume cards appear on return to previously-visited content.

---

## Permissions

| Permission | Purpose |
|------------|---------|
| `activeTab` | Read current tab URL and title |
| `scripting` | Inject overlay and content scripts |
| `tabs` | Read all open tabs for context building |
| `tabGroups` | Create and manage tab groups |
| `storage` | Save settings and activity state |
| `alarms` | Schedule focus session timers |
| `bookmarks` | Bookmark-and-close action |
| `webNavigation` | Detect tab navigation for activity tracker |
| `nativeMessaging` | Communicate with the Python daemon launcher |
| `<all_urls>` | Inject overlay on any page |

---

## WebSocket Connection

The extension connects to the daemon WebSocket at `ws://127.0.0.1:9473` and:

- Receives `STATE_UPDATE` every 500ms (updates popup state indicator)
- Receives `INTERVENTION_TRIGGER` (shows overlay)
- Sends `USER_ACTION` when the user interacts with the overlay
- Sends `ACTIVITY_SYNC` when learning progress changes
- Sends `CONTEXT_REQUEST` responses with tab data

The extension implements exponential backoff reconnection if the daemon is not running.
