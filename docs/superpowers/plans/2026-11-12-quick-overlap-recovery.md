# Quick overlap recovery implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; desktop work is assigned to GPT-6.1 Terra as requested.

**Goal:** Reproduce the 2026-11-12 real-frame failure, remove its proven cause without weakening friend identity/commit safeguards, then test locally before cross-computer release acceptance.

**Architecture:** Reuse the quick workflow, nickname foreground geometry and existing isolated scanner/database harness. Add real-evidence replay through external, read-only fixtures and targeted synthetic identity/continuity negatives. Keep real player images and text out of Git and published artifacts.

**Tech Stack:** Existing Python virtual environment, unittest, NumPy/Pillow/OpenCV, packaged OCR models; Windows desktop automation by Terra only.

**Spec:** User's five-step instruction in this chat and the evidence review at `C:/Users/goldl/.codex/outputs/gfm-t01-result-review-20261112/REVIEW.md`.

## Global constraints

- Preserve original evidence, candidate2 and official v1.1.1 bytes.
- Keep previous historical-revisit correction and all atomic-commit guards.
- Never infer stable UID from nickname or image similarity alone; reject changed identities and uncertain continuity.
- No real user database use in offline tests; no uploads, pushes or release.
- Changes follow observed failures; do not rewrite unrelated OCR/scan subsystems.
- Live testing uses a new isolated app data directory and only proceeds when the running local game is ready.
- Cross-computer acceptance is deferred until a stable release candidate.

## Review focus

- Changed/added/deleted nickname strokes must not pass as the same card.
- Same-nickname friends with different UIDs must remain separate.
- Bottom-limited motion and historical revisits must not skip rows or pending identity checks.
- Failed/ambiguous scans must retain the previously committed roster.
- Missing private evidence must be an explicit skip, never synthetic success described as real replay.

## Task 1: Offline reproduction and test-first correction

Files: new `tests/test_quick_overlap_recovery.py`; targeted changes only to `app/quick_scan.py` and, if demonstrated necessary, `app/nickname_ocr.py`; local replay results outside Git.

- [x] Run the existing suite as baseline in this isolated worktree using the existing environment.
- [x] Load the saved overlap diagnostic and original frames from the provided Readback-Evidence snapshot; validate saved field/motion facts and compare actual name/foreground geometry. Keep all originals read-only.
- [x] Add a private-fixture regression that reconstructs the real previous/current observations and exercises the real quick workflow comparison. Observe the expected failure before production changes.
- [x] Re-read the relevant real crops/pages using the existing OCR engine in isolation; record actual outputs locally and sanitized outcome summaries.
- [x] Choose the smallest correction supported by replay. Write its concrete decision and limits in the ledger before editing production code.
- [x] Verify real overlap recovery together with synthetic changed-stroke, wrong-avatar, known-name conflict and incompatible-card negatives. Preserve unknown identity until UID verification.

## Task 2: Continuity, identity and persistence regression

Files: reuse `tests/test_quick_revisit_scroll.py`, `tests/test_quick_scan_workflow.py`, `tests/test_quick_identity_storage.py`; extend the new focused tests where an uncovered behavior exists.

- [x] Test pending-to-readable and readable-to-pending overlaps, bottom-limited movement, and late revisit after expected row count.
- [x] Confirm duplicate UID, same-name distinct UID, reordered rows, stalled movement and incomplete verification cannot cause a false full commit.
- [x] Run the entire existing suite with real private replay enabled; report exact pass/fail/skip counts and skip causes. Check the source diff and evidence hashes.
- [x] Obtain one independent read-only review of the final code/tests and address important findings with failing-then-passing tests.

## Task 3: Local real-game validation and handoff

- [x] Have GPT-6.1 Terra check the local game's availability; do not change credentials, privileges or system security.
- [x] When offline tests are sound, run the corrected source or a separately built test candidate with fresh config/data/logs. Never overwrite the user's installed/released copy.
- [ ] If the game is ready, execute one single quick scan, observe overlap/identity revisit, then inspect complete/committed, counts and identity safety. Preserve evidence locally. If unavailable, record the precise blocker without claiming live success.
- [ ] Record source diff, replay coverage, tests, remaining risks and local live outcome. Leave cross-computer acceptance to the stable pre-release stage.
