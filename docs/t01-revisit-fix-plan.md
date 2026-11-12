# T01 identity revisit investigation and fix plan

User requirement: diagnose the v1.1.1 scan abort after 179/179 rows, reproduce before changing code, preserve identity verification and atomic commit, and leave original evidence and released packages untouched. Do not change profile OCR or operate the game.

Base: v1.1.1, commit f849e5c01a3ed36417ba23c65c2363f1858f41f9. Independent clone and branch codex/fix-identity-revisit-alignment; the dirty GenshinMusic checkout is outside this work.

## Evidence and constraints

- T01/F01/F02 reports are readable under E:/GenshinFriendMonitor测试. The original C:/新建文件夹/.../T01-20261111-194457 snapshot is unavailable here.
- F01/F02 retained scan.log and ocr_dump.log have the exact hashes recorded for T01. Use them read-only for the chronological trace; do not copy private logs into the repository.
- Missing: workflow-latest.json, the original page 36/37 frames, movement.jsonl, alignment frames and failed movement pair. Full T01 image replay remains unavailable unless supplied separately.
- A simulated regression can prove a code defect and a bounded correction, but cannot certify the original T01 screenshots or real-game completion.

## Working hypothesis to test

QuickScanWorkflow.verify_late_ambiguities resets offset/origin to revisit old rows while retaining the complete slots map. FastScan._advance currently compares the next image against max(slots)+1, the end of the entire collected roster. An otherwise valid revisit beyond the home page can therefore be classified as no_new_rows, retried twice and rejected. Need to prove this with the existing synthetic image/drag simulator before editing production code.

## Tasks

1. Run the original full test suite as baseline. Establish dependency availability without installing or updating packages.
2. Add focused regression tests using synthetic inputs: a 179-row late conflict outside the home page; smaller boundary/bottom variants; previously committed identities; blocked revisit movement; conflicting UID/reordered historical row and bounded revisit exhaustion. Assert necessary UID checks happen before commit and failures preserve prior database content.
3. Watch the new positive regression fail with the T01 message, then make the smallest change supported by that failure. Prefer deriving the next viewport row from the current measured viewport rather than discarding collected identities or bypassing completeness guards.
4. Run focused tests and the existing complete suite. Count passed/failed/errors/skipped. Check that movement, overlap, identity, transaction and failure guards remain active.
5. Obtain independent code review, address important findings with tests, and write a root-cause/evidence/limitations report. Produce a candidate only if its build and available verification are meaningful; no push, merge or release.

## Review focus

- A historical viewport must advance through known rows without requiring an uncollected global row.
- Ordinary traversal must still reject a real skipped row, no movement, reverse movement and low confidence.
- Completing row count must not bypass pending identity checks, final header/account checks or atomic commit.
- A failed/reordered/conflicting-UID revisit must preserve all prior records and events.
- No original screenshots, real identities, databases or credentials enter the patch, tests or candidate archive.

## Progress

- Reports and retained log hashes checked; no original movement images or workflow JSON accessible.
- Isolated source clone and repair branch created; native cross-repository worktree attachment rejected its arguments, so the independent clone provides isolation.
- Initial baseline: 805 tests, 2 launcher failures, 99 skips because the clone had no .venv/Scripts/python.exe. Created a local venv with existing system site-packages and no pip/install/update; rerun baseline: 805 tests, 706 passed, 99 skipped, 0 failures/errors.
- RED: synthetic viewport with 179 existing slots rejected valid historical motion with the exact T01 error and corrections [40.0, 40.0]. Synthetic 179-row end-to-end revisit failed at page 37 (eventually low correlation after unnecessary corrective inputs).
- Minimal patch under verification: alignment derives next_row from the last complete row of the current viewport, preserving the existing gap/movement/identity guards.
- GREEN: focused suite 60/60 passed. Added an eighth regression for reordered historical observations; full suite 813 total, 714 passed, 99 private-fixture skips, 0 failures/errors (78.165s).
- Eight new tests cover the exact synthetic two-40px failure, synthetic 179-row first commit, small/partial-bottom boundaries, stable existing identities, stalled revisit, duplicate verified UID, reordering, and exhausted revisit limit.
- Independent read-only review dispatched. Building a separately named candidate from installed dependencies; no pip install/update or release command used. Actual T01 image replay and real-game validation remain NOT_RUN.
- Independent reviewer: no Critical/Important/Minor findings; all 8 new tests independently passed. Unjudged: actual missing T01 replay, skipped private fixtures and candidate runtime; these remain explicit validation limits, not accepted coverage claims.
- Candidate1 rejected: build succeeded but fontTools was absent and the license packager failed. Kept its logs; do not distribute that EXE. Installed the exact requirements-local-lock.txt versions only into this clone's .venv (global packages untouched), and started a fresh candidate2 build plus a full-suite rerun under the locked runtime dependencies.
- Final validation: locked-environment suite again 813 total / 714 passed / 99 skipped / 0 failures/errors. Candidate2 packaged with 58 license texts, exact lock versions and no private runtime data; ZIP and AMD64 checks passed.
- GPT-6.1 Terra performed only new-candidate desktop smoke: two starts and normal exit codes 0, synthetic OCR UID/name/level correct, cancelled dialog gone, SQLite integrity ok/friends=0. Synthetic signature remained unreliable as in prior baseline. No game was operated. Smoke is on a non-clean host with network unchanged.
- Actual T01 original-image replay and real-game completion remain NOT_RUN. Original release SHA256 rechecked unchanged. Local source/patch/candidate delivered without push or release.
