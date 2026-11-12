# Window Capture and Administrator Startup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the selected game window independently of desktop occlusion and request administrator startup with clear failure handling.

**Architecture:** A Qt-main-thread capture broker returns a fresh frame from an HWND-specific session to existing scan workers. A separate privilege module reuses the existing runas helper before GUI startup; the main window independently warns and guards against missing elevation.

**Tech Stack:** Existing Python, PySide6 >=6.10, NumPy, pywin32, Windows UAC, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-11-12-window-capture-admin-design.md`.

## Global Constraints

- Preserve existing player/test evidence, released ZIP/EXE and unrelated user changes.
- No relaxed UID/roster validation, no automatic scans, no background game input.
- Window-capture failures must not fall back to desktop rectangles.
- Desktop operations use GPT-6.1 Terra; unredacted game images stay local.
- Existing installed dependencies only during development; include required Qt Multimedia components in future packaging.

## Review Focus

- A delayed old frame or a frame from another HWND must never satisfy a new request.
- DPI, decorations, negative screen coordinates and window movement must not silently shift the OCR crop.
- Timeout/closing/concurrent requests must not hang the GUI or leave capture resources alive.
- UAC cancellation and a relaunch that remains unelevated must stop without loops or scanning.
- Source launchers and packaged one-file EXEs must preserve arguments and use their own data directories.

## Task 1: Administrator startup and visible guard

**Files:** new `app/privileges.py`, `tests/test_admin_startup.py`; existing `admin_start.py`, `app/ui/main_window.py`, isolated UI test fixtures as needed. Parent owns `main.py` and `build.spec` integration.

- [x] Add and run failing tests for token detection, current-executable source/frozen relaunch, quoting, cancelled/failed UAC and retry limits.
- [x] Reuse `_run_as`; implement `ensure_administrator() -> bool` before GUI startup.
- [x] Show a clear main-window warning and guard game actions when instantiated without an administrator token; isolate ordinary UI tests from host permissions.
- [x] Run focused privilege, launcher and UI tests; independently review this subsystem.

## Task 2: Window capture backend and routing

**Files:** new `app/window_capture.py`, `tests/test_window_capture.py`; modify `app/capture.py`, `app/scanner.py`, `app/fast_scan.py`, `app/account_identity.py`, `main.py`, `requirements.txt`, `build.spec`.

- [x] Add failing tests for target routing, no desktop fallback, fresh RGB frames, bounds/resize/minimized checks, bounded waits and cleanup.
- [x] Implement the main-thread Qt broker and HWND capture sessions, returning owned RGB arrays with exact client cropping.
- [x] Route all game captures through explicit HWND scope while retaining the existing rectangle API and injectable offline inputs.
- [x] Initialize broker in the GUI entry point, retain manual-region semantics, and include Qt Multimedia in packaging.
- [x] Run targeted capture, identity and scan regressions; inspect actual PyInstaller hook behavior.

## Task 3: Verification and local candidate

- [x] Add a red→green input hit-test regression for nonactivating occluders: root HWND checks before mouse down/wheel, rechecks after moving/during dragging, unconditional release, legacy paths included; preserve explicit manual-region mode.
- [x] Run the full suite, fix regressions and request independent review.
- [x] Have GPT-6.1 Terra verify occlusion using synthetic/native windows where available; record real-game read-only capture separately.
- [x] Prepare a new independent local test copy and verify source/launcher identity; do not mutate earlier candidates/data.
- [x] Report passes/failures/skips, actual capture backend evidence, remaining limitations and manual retest steps.

## Execution rulings

- Ruling: admin startup and capture are independent components and are implemented in parallel with disjoint ownership; parent owns entry/build integration and final evidence — avoids two implementers editing the same UI/capture files.
- Ruling: allow at most three asynchronous redraw requests only for the selected live target — real static-window evidence proved Qt can discard its first frame; no game state, focus, geometry or keyboard/mouse action changes.
- Ruling: add input root hit-testing before delivery — independent all-mock review reproduced global mouse events going to a nonactivating occluder while foreground remained the game; capture correctness alone does not make those input events safe.
- Evidence: administrator component 100 focused tests PASS; capture component 31 new tests PASS; parent integration 4 tests first failed then passed; input safety 20 new tests PASS; final full 917 tests: 812 PASS, 105 private fixture skips, no failures/errors. Independent review's initial P1 was fixed and re-reviewed with eight additional pure-mock integration probes.
- Desktop evidence: static/dynamic synthetic occlusion PASS; real game read-only 2560×1440 frame PASS; after the user restored an eligible window, real-game central occlusion PASS, with matching cover hit-test, zero cover-color pixels in captured center and unchanged foreground. No game scan/input, OCR or raw-game-frame saving performed.
