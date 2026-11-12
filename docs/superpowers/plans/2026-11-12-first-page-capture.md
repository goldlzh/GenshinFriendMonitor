# First-page capture implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline, task by task.

**Goal:** Restore desktop capture by default and reject incomplete/off-screen game clients before recognizing or acting on an incomplete frame.

**Architecture:** Keep the existing four-argument capture injection boundary. Selected-window captures validate current physical client geometry against one physical monitor, then use desktop/mss by default. WGC is opt-in through `GFM_EXPERIMENTAL_WINDOW_CAPTURE=1`; native failures never fall back silently. Admin startup and input ownership protections remain enabled.

**Tech Stack:** Existing Python, pywin32, mss, numpy, PySide6 and unittest; no dependency installation.

**Spec:** User's instruction to pause occlusion capture and fix the first-page failure; design and evidence below.

## Evidence and design

The saved failed frame has six complete row bands but no scrollbar at normalized x=1804. Its recorded client rectangle extends 336 physical pixels beyond a 3840-pixel screen. The resulting normalized right boundary is x=1668; all six card-background probes exhibit an abrupt boundary there. This strongly supports clipping of UI content at the visible screen edge, but does not establish which game/driver/compositor stage causes it.

New fully on-screen desktop/mss samples all detect a scrollbar at x=1804, y=117..215 using the unchanged detector; three source hashes match their reports. This is a capture/layout observation, not a completed game scan. Do not change detection thresholds or declare completion from visible row count.

## Global constraints

- Existing experimental branch/commit remains intact; work on `codex/fix-first-page-capture`.
- Original game evidence is read-only and stays local. No private images or player fields in Git.
- No automatic game input, scans, desktop changes, builds, uploads or releases.
- Preserve OCR, identity validation, transaction retention and full-pass commit checks.
- Manual rectangle injection and offline replay remain supported.

## Review focus

- Negative-coordinate secondary monitors must work when the client fits inside one monitor.
- A rectangle spanning displays, outside all displays, stale, minimized, or unavailable must stop clearly.
- Default startup must not create a Qt native capture session.
- Explicit WGC failures must not become desktop captures.
- A capture error must propagate through existing failure/rollback paths without committing a partial roster.

## Task 1: Default desktop capture and explicit geometry failures

**Files:** `app/capture.py`, `app/window.py`, `tests/test_capture_preflight.py`, `tests/test_window_capture.py`.

- [x] Run the existing full suite and retain the baseline output.
- [x] Add failing behavior tests for default desktop routing, skipped native startup, known off-screen client, valid/negative monitor coordinates, stale/minimized windows and explicit-native failure isolation.
- [x] Observe assertion failures before implementation.
- [x] Add `window.capture_rect_error(hwnd, rect)` and `capture.CaptureError`; validate geometry before and after selected-window captures.
- [x] Gate native broker creation/routing with the explicit experimental environment flag; keep existing injection semantics.
- [x] Run focused capture/admin and existing scan retention regressions, then the full suite.

## Task 2: Offline evidence and manual retest handoff

- [x] Replay the failed frame and new samples with the unchanged detector. Preserve originals and report pure capture times separately from composite home-probe timing.
- [x] Review the diff and confirm no scan/OCR/database algorithm changes.
- [ ] Prepare an independent local source candidate only if automated checks pass, using the existing launcher/environment and isolated data.
- [ ] Ask for one manual unobscured quick scan with the entire client on-screen; verify fresh workflow completion/commit and roster accuracy afterward. Do not claim this step passed in advance.
- [ ] Keep GitHub push pending this final real-game result; never push private evidence or release directly.

## Validation ledger

- Baseline: 917 run, 812 pass, 105 skip, 0 failures.
- Initial behavior regressions: 10 run, 9 expected assertion failures before production changes.
- Review found two P2 issues: missing native postflight and continuous retries after a geometry error. Added failing tests, then fixed postflight and stopped the active task in `Scanner._run` and `FastScan.shot` error paths. Identity/commit algorithms remain unchanged.
- Review regression run: 14 tests, four expected assertion failures (including two frequency subtests) before fixes.
- Final focused capture/admin: 49 pass, 0 failures. Final full suite: 931 run, 826 pass, 105 skip, 0 failures, 90.262 seconds.
- Fresh-context follow-up review: no remaining P1/P2 findings. No reviewer desktop or private-data access.
- Ruling: native capture remains opt-in in this branch rather than deleting its module. The existing experimental branch/commit is preserved, and ordinary startup neither constructs a native session nor imports its module. This avoids coupling the stable default to experimental frame acquisition.
- Real complete game scan remains pending manual execution; neither regression tests nor three successful geometry samples satisfy that gate.
