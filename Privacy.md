# Privacy

## What Cortex Collects and Where It Goes

Cortex is designed to keep biometric data on your machine. Here is an exact account of every piece of data the system handles.

---

## Webcam / Biometrics

| Data | What happens |
|------|-------------|
| **Video frames** | Processed in memory at 30 FPS. Never written to disk. Never sent anywhere. |
| **Face images** | Used to extract landmarks and RGB traces. Immediately discarded after processing. |
| **Heart rate (BPM)** | Computed locally. Stored in memory for state classification. Never sent to the LLM or any external service. |
| **HRV (RMSSD)** | Same as heart rate — local only. |
| **Blink rate** | Local only. |
| **Head pose / posture** | Local only. |

**No biometric data ever leaves your machine.**

---

## Behavioral Telemetry

| Data | What happens |
|------|-------------|
| **Mouse movements** | Aggregated to velocity/jitter statistics at 1 Hz. Raw events are not stored. |
| **Keyboard activity** | Aggregated to burst scores and interval variance. **Keystroke content is never captured** — only timing patterns. |
| **Window/tab switches** | Switch rate is tracked (count per minute). Titles are read for context only when an intervention is triggered. |

---

## What the LLM Sees

When Cortex triggers an intervention, it sends a context packet to the LLM. This contains **only workspace context**:

- Current cognitive **state label** and **confidence** (e.g., `HYPER 0.91`)
- Active **file path** and **visible code range** from VS Code
- Active **diagnostics** (error messages, line numbers) from VS Code
- **Tab titles and URLs** from Chrome (sampled, max 30)
- **Recent terminal output** (text only, not command history)

The LLM never receives:
- Heart rate, HRV, or any physiological measurement
- Video frames or face images
- Mouse/keyboard content
- Clipboard contents

---

## Data Storage

| Storage | What | Location |
|---------|------|----------|
| **Baselines** | Calibrated resting HR, HRV, blink rate, posture | `storage/baselines/` — local only |
| **Session features** | Aggregated feature vectors (7-day retention) | `storage/features/` — local only |
| **Activity tracker** | Learning progress (platform, URL, position) | `storage/activity/` — local only |
| **Bandit weights** | Learned intervention preferences | `storage/eval/` — local only |
| **Error logs** | Crash reports and exceptions | `storage/errors/` — local only, 90-day retention |

Redis is used if available, with automatic in-memory fallback. No cloud sync.

---

## API Key Security

The Azure OpenAI API key can be stored in `.env` (development) or in macOS Keychain (recommended for production):

```bash
security add-generic-password -s cortex.azure_openai -a default -w YOUR_KEY
```

Then in `.env`:
```bash
CORTEX_LLM__AZURE__USE_KEYCHAIN=true
```

The key is never logged, never included in WebSocket messages, and never sent to the extension.

---

## Browser Extension Permissions

The extension requests broad permissions to function. Here is exactly why each is needed:

| Permission | Why |
|------------|-----|
| `activeTab` | Read current tab URL for context building |
| `scripting` | Inject the intervention overlay |
| `tabs` | Read all tab titles/URLs for the LLM context packet |
| `tabGroups` | Create named tab groups (intervention action) |
| `storage` | Save quiet mode state, bandit feedback, activity |
| `alarms` | Schedule focus session end notifications |
| `bookmarks` | Bookmark-and-close action |
| `webNavigation` | Detect navigation for the activity tracker |
| `nativeMessaging` | Start/stop the Python daemon |
| `<all_urls>` | Inject the intervention overlay on any page |

---

## Consent-Gated Autonomy

Cortex cannot take any autonomous action without earned trust. The consent ladder has 5 levels per action type:

```
OBSERVE → SUGGEST → PREVIEW → REVERSIBLE_ACT → AUTONOMOUS_ACT
```

- Cortex starts at **SUGGEST** — it shows recommendations but takes no action
- 5 user approvals of the same action type escalate to the next level
- 3 user rejections de-escalate by one level
- No tab is ever closed, no code is ever folded, without the user either approving in real-time or having previously granted that action type sufficient trust

All interventions are reversible. Workspace state is snapshotted before any action and can be restored with one click.
