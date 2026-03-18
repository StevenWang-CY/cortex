# Changelog

## [Unreleased] — Full Revision (2026-03-18)

Comprehensive audit-driven revision across signal processing, LLM pipeline, Chrome extension, LeetCode mode, and new features. 903 tests passing.

### Wave 1 — Critical Fixes

**Signal & State Engine (1A)**
- C-01: Raised SQI validity threshold from 0.1 → 0.4 to reject noisy heart rate estimates
- C-02: Added parabolic peak interpolation to `peak_detection.py` — eliminates 33ms IBI quantization error at 30 FPS
- C-03: IBI count guard requires ≥5 inter-beat intervals before propagating RMSSD
- C-04: Fixed s6 blending — weighted blend (60% thrashing + 40% switch) instead of `max()` which double-counted velocity
- C-10: Shutdown detector late hour configurable (default 23:00), all thresholds now constructor parameters
- C-11: Resolved YAML vs Pydantic default discrepancies (complexity_threshold, quiet_mode_minutes)
- O-09: Context-aware blink suppression — attenuates score 0.3× when HR is normal (concentration, not stress)

**LLM & Intervention Pipeline (1B)**
- C-05: Fixed KeyError in v2 templates by adding `extra_context` parameter to `build_user_prompt()`
- C-06: Nuanced staleness check — suppresses intervention only on genuine FLOW recovery (≥3s dwell), checks tab snapshot validity
- W-11: Fixed `is_destructive` false positive — checks `action_type` field instead of substring matching on label text
- W-14: Added `GET /consent/level` and `POST /consent/reset` API endpoints

**Chrome Extension (1C)**
- C-08: First-run onboarding page with 3-step setup guide
- C-09: Debounced SW state persistence (500ms trailing timer) to `chrome.storage.session` — survives service worker restarts
- W-25: Tab-manager snapshot persistence across SW restarts
- Popup shows disconnected banner when daemon not running

**LeetCode Mode (1D)**
- C-07: Lockout overlay renderer with countdown timer and skip button (no penalty — bandit learns naturally)
- C-12: `LeetCodeAdapter` now inherits `CortexAdapter` protocol
- W-21: Zombie detector returns `False` when `blink_rate is None` instead of falling through to `True`
- W-28: Parasympathetic rebound temporal guard — only detects within 5 minutes of last acceptance

### Wave 2 — Experience Improvements

**LLM Quality & Bandit (2A)**
- W-08: Structured JSON `response_format` added to Azure OpenAI calls
- W-12: LinUCB regularization increased 1.0 → 5.0 for better cold-start exploration
- W-13: Helpfulness reward redistributes 30% weight proportionally when no explicit rating
- Token budget hard cap with 3-pass truncation (terminal → tab titles → code content)
- W-18: Deprecated `InterventionTrigger` in favor of `TriggerPolicy` with deprecation warnings

**Prompt Engineering & Tab Classifier (2B)**
- O-08: New `tab_classifier.py` — single source of truth for domain-based tab type classification
- W-05: Parser enforces keep on AI assistant and documentation tabs via classifier (not hardcoded URLs)
- W-26: Fixed active recall template biometric contradiction
- W-10: Added normalizer defaults to generic-phrase blocklist
- s6 same-category discount — switching between tabs of the same type gets reduced penalty

**Detector Robustness & Config (2C)**
- W-06: Removed goal-relevant verbs from rabbit hole stop-word list ("implement", "build", "create", "fix", "add")
- W-15: Removed overlay backdrop dim (transparent instead of 35% opacity)
- W-16: Chrome extension cooldowns sync with daemon config via SETTINGS_SYNC
- W-17: Quiet mode toggle added to Chrome popup
- W-19: Adaptive solution friction with exponential decay (`max(10, 60*exp(-t/600))*difficulty_mult`)
- W-20: Fixed zombie detector docstring/code mismatch

### Wave 3 — Innovation Features

- O-10: Pre-break warning at 80% stress threshold via `should_warn()`
- O-01: `SessionReportGenerator` — full biometric study session reports with flow percentage, state transitions, golden hour, 7-day comparison
- O-04: Per-topic difficulty tracking in `LongitudinalTracker` with stress modifier calibration
- O-07: Modality preference tracking in `TabRelevanceTracker` for proactive resource switching

### Test Coverage

- 903 tests passing (up from ~870 pre-revision)
- New test files: `test_session_report.py`, `test_tab_classifier.py`, `test_rabbit_hole.py`, `test_stress_integral.py`, `test_zombie_detector.py`, `test_parasympathetic_rebound.py`
- Pre-existing failures (not caused by revision): `test_activity_tracker.py` (redis import), `test_redis_store.py` (redis import), `test_context_to_llm.py` (system prompt contains "heart rate" in instructions), `test_scripts.py` (9 failures)
