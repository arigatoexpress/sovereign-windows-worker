# sovereign-agent-worker — charter

24/7 self-driving maintenance engineer on the Windows RTX 5070 Ti box.
Task: `sovereign-agent-worker` (S4U, boot/logon, restart×3). Code: `worker.py`. Tests: `test_worker.py` (11+ tests).

## What it does
1. Executes `queue\*.md` tasks: aider (qwen3-coder:30b, on-box Ollama) → task tests →
   2-attempt failure-feedback loop → regression gate → report + git bundle in `reports\`.
2. When idle (every 12h): regression sweep over allowlisted repos, then **backlog mining** —
   it generates its own work from repo signals:
   - LOW risk (auto-queued, ≤2 queued / ≤4 per day): single-file ruff-violation fixes.
   - MEDIUM risk (→ `proposed\`, needs approval): sweep failures, TODO/FIXME resolutions.

## Hard rules (by construction — do not relax)
- Only ALLOWLIST repos (worker.py): Sapphire, telemetry-dashboard, claw-code.
  **Project-Go-Forward (THO) is permanently excluded.**
- Commits land on `worker/*` branches only. The worker NEVER pushes or merges —
  this box holds no GitHub credentials on purpose.
- No outward network actions. Local Ollama + local git only.
- Runaway guard: any diff > 3000 lines fails the task.
- Regression gate: diffs touching non-test files must pass the repo quick suite.

## Review loop (Mac side)
- `sov worker status | reports | show <r> | proposed | approve <t> | pull`
- `sov worker pull` copies reports into `~/ops-state/worker-reports/` and the Knowledge
  vault (`6-Agent-Memory/worker/` — becomes searchable institutional memory), runs
  every 6h via launchd `com.ari.worker-sync`.
- To land a worker branch: fetch its `.bundle` from the reports dir into a Mac clone,
  review, push from the Mac. Example:
  `git fetch ~/ops-state/worker-reports/<date>-<task>.bundle worker/<task>:worker/<task>`

## Task file format
```
repo: C:\Users\aribs\Code\Sapphire
test: python -m pytest tests/unit/test_x.py -q
---
Goal text for the coding agent. Multi-line is fine (keep blank line before ---? no —
header block ends at the first line that is exactly `---`).
```

## Operations
- Logs: `worker.log` (wrapper, auto-rotated at 100 MB) + `reports\*.md` (per task/sweep,
  timestamped) + `heartbeat.json` + `metrics.json`.
- Restart: `schtasks /end /tn sovereign-agent-worker & schtasks /run /tn sovereign-agent-worker`
- The worker reloads code only on restart — after editing worker.py, restart the task.
- Recovery: if a repo was left dirty, the worker stashes before each task and restores the
  original branch + pops the stash in a `finally` block. Check `git stash list` if something
  looks missing.

## Environment overrides (optional)
- `SOV_WORKER_PYTHON` — Python executable (default: `C:\Users\aribs\AppData\Local\Programs\Python\Python313\python.exe`)
- `SOV_WORKER_AIDER` — aider executable (default: `C:\Users\aribs\.aider-venv\Scripts\aider.exe`)
- `SOV_WORKER_MODEL` — primary coding model (default: `ollama/qwen3-coder:30b`;
  selected by a Windows contract canary where it passed on attempt 2 and Devstral did not)
- `SOV_WORKER_WEAK_MODEL` — weak model (default: `ollama/gemma3:4b`)
- `SOV_WORKER_MAP_TOKENS` — bounded repo-map budget (default: `512`)
- `SOV_WORKER_MAX_CHAT_TOKENS` — bounded Aider chat history (default: `8192`)
