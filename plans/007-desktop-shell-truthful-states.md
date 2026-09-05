# 007 — Desktop shell: truthful states, session semantics, window posture

Severity: HIGH · Status: DONE · Depends on: 005, 006

## Problem

The macOS shell (`cortex/apps/desktop_shell/`) shipped several states that
were not true or not safe:

- "View full report" on the recap sheet quit the app; "Stop Cortex" (Cmd+Q)
  was really Quit; ending a session had no route that kept Cortex open.
- Windows inherited the system dark theme half-way (dark text on light cards).
- The suggestion card activated itself, listened to bare `S`/`Q` keys, sat at
  460 px, and showed the breathing pacer for every plan.
- Indicators lied: "Connected" used the FLOW colour, extension dots had no
  text, "Sessions 1", "Best 0s", a permanent `$—` cost pill, "cost so far
  today: pending…", a palette combo that did nothing but demanded a restart,
  "Get Started" marking six steps complete regardless, a progress strip that
  never advanced, and a WS-mode tray naming a state with no evidence.
- Sub-AA text (systemGreen "Granted", 11 px BIO tints), missing focus rings,
  no Escape on Settings/Connections, thirteen modal message boxes in
  Connections, timers running on hidden windows, and widget calls from the
  daemon thread.

## Changes

| Area | File(s) | Result |
| --- | --- | --- |
| Session routes | `dashboard.py`, `recap_sheet.py`, `controller.py`, `main.py`, `tray.py` | End session → ended phase (Start session / Quit Cortex); recap sheet: View full report / Close / Quit Cortex; `_arm_stop(quit_after=True)` only for tray / Cmd+Q; in-process daemon restart on `session_start_requested` |
| Appearance | `mac_native.py`, `controller.py`, `main.py` | `apply_vibrancy(widget, appearance=)` pins Aqua per window; `application_palette()` on the QApplication; `Material` removed |
| Suggestion card | `overlay.py`, `tokens.yaml` | `WA_ShowWithoutActivating`, no `activateWindow`, Cmd shortcuts, Escape when focused, `POPUP_WIDTH` top-right of cursor screen, pacer only for breathing plans, single "Why this?" toggle, `suppress()` stops timers |
| Truthful indicators | `dashboard.py`, `settings.py`, `onboarding.py`, `tray.py` | info-colour Connected dot, labelled extension dots, stats hidden until data, cost hidden until data, live palette swap, honest onboarding completion + progress strip, evidence-aware tray status |
| Contrast / focus / press | `sync_design_tokens.py` → `tokens.py`, `components.py`, all surfaces | `*_TEXT` tokens for status text; `BTN_*_QSS`, `PILL_*_QSS`, `INPUT_QSS`, `HERO_NUMERAL_QSS` recipes with reserved focus border and `:pressed` |
| Windows | `settings.py`, `connections.py`, `onboarding.py`, `history_tab.py` | Escape closes; no Back; Settings scrolls with disclosures (schedule, Advanced); Connections inline checklist + re-probe on show, no `xattr` advice |
| Thread safety | `controller.py`, `main.py` | `DaemonBridge.ui_task` replaces `QTimer.singleShot(0, …)` from the asyncio thread; WS-mode calibration progress goes through the queued signal only; calibration activation runs in `asyncio.to_thread` |
| Consolidation | `components.py` | one `SegmentedControl`, `Disclosure`, floating `Toast`, `format_relative_age`, status pill/dot recipes; private `_WINDOW_BG/_LABEL/_SEPARATOR` alias blocks deleted |

## Evidence

- `python -m cortex.scripts.sync_design_tokens --check`
- `ruff check cortex/`, `mypy --config-file cortex/pyproject.toml cortex/ --strict`
- `QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit` including the new
  `test_desktop_shell_contracts.py`, the rewritten `test_dashboard_stop.py`
  and `test_overlay_causal_truncation.py`, and the extended
  `test_ui_contrast_contract.py`
- `CORTEX_LEGACY_QT_ISOLATED=1 QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_desktop_shell.py`

Screenshots were not taken: the package forbids launching the GUI on a real
screen; every contract above is pinned by an offscreen test instead.
