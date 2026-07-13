# THO Explicit Request Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guarded, explicit-task-only workflow that lets the Windows worker process normalized Mark-at-THO requests from a Mac-supplied, verified current Git bundle without admitting THO to autonomous sweeps or touching the stale dirty Windows clone.

**Architecture:** Normal `ALLOWLIST` execution remains unchanged and continues to exclude Project-Go-Forward. A task tagged `source: gmail-mark-tho` must include a 40-character `base_sha`, a `.bundle` located under the worker's inbound bundle directory, and a non-empty test command; the worker verifies the bundle, clones it into an isolated per-task workspace, checks out the exact SHA, runs aider on a `worker/*` branch, applies tighter diff/path/test gates, emits its report/bundle, and deletes only the generated workspace. A Mac-side staging script creates the source bundle and provenance-bearing task artifact; it never reads Gmail itself or sends email.

**Tech Stack:** Python 3.13+, stdlib (`argparse`, `pathlib`, `subprocess`, `shutil`), pytest, git bundles.

## Global Constraints

- `Project-Go-Forward` must remain absent from `ALLOWLIST`, `mine_backlog()`, and `sweep()`.
- Only tasks with exact source `gmail-mark-tho` may enter the THO lane.
- THO tasks require `message_id`, `message_date`, `base_sha`, `base_bundle`, and a non-empty `test` command.
- `base_sha` must be exactly 40 lowercase hexadecimal characters and must equal the checked-out workspace HEAD.
- `base_bundle` must resolve beneath `BASE / "incoming-bundles"`, exist, use suffix `.bundle`, and pass `git bundle verify`.
- The existing Windows clone at `C:\Users\aribs\Code\Project-Go-Forward` must never be stashed, switched, cleaned, or modified by this lane.
- THO work occurs only in `BASE / "tho-workspaces" / <task-slug>` created from the verified bundle and removed after reporting.
- THO changed paths must reject `tho_documents/`, `.github/workflows/`, `.env`, credential/key files, and paths outside the isolated repo; THO diffs are capped at 500 changed lines.
- THO source changes must pass both the task test and the canonical quick suite `python -m pytest tests/test_healthz.py tests/test_api_v1.py tests/test_document_engine.py -q`.
- The worker never pushes, merges, deploys, changes DNS, sends email, or stores Gmail credentials.
- Generated results remain `worker/*` branches, reports, and verified git bundles for Mac-side review.

---

### Task 1: Guarded THO Explicit-Request Workflow

**Files:**
- Modify: `worker.py`
- Modify: `test_worker.py`
- Create: `stage_tho_request.py`
- Create: `THO_WORKFLOW.md`

**Interfaces:**
- Consumes: a Mac clone of Project-Go-Forward, a normalized request text file, Gmail message ID/date, and an explicit test command.
- Produces: `stage_tho_request.stage_request(...) -> tuple[Path, Path]`; parsed task fields `source`, `message_id`, `message_date`, `base_sha`, `base_bundle`; worker helpers `is_tho_task(task)`, `validate_tho_task(task)`, `prepare_tho_workspace(task)`, and `validate_tho_changed_paths(repo, base)`.

- [x] **Step 1: Write failing golden tests**

Add tests proving: generic THO remains rejected; missing/incorrect provenance is rejected; bundle path traversal is rejected; stale/mismatched SHA is rejected; a valid Mac-created bundle prepares an isolated exact-SHA workspace; prohibited paths fail; safe source/test paths pass; `ALLOWLIST` remains THO-free; the staging script emits a verified bundle and round-trippable task with the required headers.

- [x] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest test_worker.py -q`

Expected: new tests fail because the THO-lane functions and staging script do not exist.

- [x] **Step 3: Implement the minimal staging and worker gates**

Extend task parsing with the required provenance fields. Preserve `repo_allowed(repo)` as the generic allowlist guard. In `run_task`, route only valid exact-source THO tasks through an isolated verified-bundle workspace, enforce the 500-line/path/mandatory-test/canonical-regression gates, create the result bundle before cleanup, and remove only the generated THO workspace. Keep general mining/sweeps unchanged. Implement the Mac staging CLI without Gmail/network dependencies.

- [x] **Step 4: Document the operational workflow and fences**

Document: Gmail search/read and normalization happen on Mac/cloud; stage the exact `origin/main` SHA/bundle; transfer the two artifacts to the worker inbound/queue directories; Windows builds only in the isolated workspace; Mac pulls, verifies, reviews, and handles any push/PR/merge; replies remain drafts until explicit approval.

- [x] **Step 5: Run GREEN verification**

Run: `python3 -m pytest test_worker.py -q`

Expected: all tests pass.

- [x] **Step 6: Commit explicit paths**

Run: `git add worker.py test_worker.py stage_tho_request.py THO_WORKFLOW.md docs/superpowers/plans/2026-07-13-tho-explicit-request-lane.md && git commit -m "feat: add guarded THO request lane"`
