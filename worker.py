"""sovereign windows worker v2 — self-driving 24/7 maintenance engineer.

v1 executed hand-fed tasks. v2 closes the loop from first principles: verification
GENERATES work, so the worker mines its own backlog from repo signals, executes the
low-risk classes autonomously, proposes the rest for Mac approval, and guards every
change against collateral damage.

Loop:
  1. queue\\*.md      — execute (aider on the local GPU -> task tests -> regression gate)
  2. queue empty      — every SWEEP_INTERVAL_H: regression sweep + backlog mining
  3. every result     — report + git bundle in reports\\ for Mac-side review/push

Backlog mining (per allowlisted repo), risk-tiered:
  LOW    -> auto-queued (cap MAX_AUTO_QUEUED at once, MAX_AUTO_PER_DAY/day):
           * ruff violations in one file -> "fix lint in <file>"
  MEDIUM -> proposed\\ only (Mac approves by moving into queue\\ / `sov worker approve`):
           * sweep test failures, TODO/FIXME items

SAFETY (unchanged, by construction): ALLOWLIST repos only (THO excluded); worker/*
branches only; NEVER pushes (box has no creds); no outward network. New in v2:
  * runaway guard — diff > MAX_DIFF_LINES lines fails the task
  * regression gate — source-touching diffs must pass the repo quick suite
  * auto-task caps — bounded self-generated work, tests-and-lint classes only

Task file format (queue\\<name>.md):
    repo: C:\\Users\\aribs\\Code\\Sapphire
    test: python -m pytest tests/unit/test_x.py -q
    ---
    <goal for the coding agent>
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
from pathlib import Path

HOME = Path.home()
BASE = HOME / "agent-worker"
QUEUE = BASE / "queue"
PROPOSED = BASE / "proposed"
DONE = BASE / "done"
FAILED = BASE / "failed"
REPORTS = BASE / "reports"
HEARTBEAT = BASE / "heartbeat.json"

AIDER = HOME / ".aider-venv" / "Scripts" / "aider.exe"
PYTHON = HOME / "AppData" / "Local" / "Programs" / "Python" / "Python313" / "python.exe"
MODEL = "ollama/codestral:22b"          # non-thinking, FIM diff-trained, fits 16GB
WEAK_MODEL = "ollama/gemma3:4b"

ALLOWLIST = [
    HOME / "Code" / "Sapphire",
    HOME / "Code" / "telemetry-dashboard",
    HOME / "Code" / "claw-code",
]
# NEVER add Project-Go-Forward (THO client prod fence) or any repo with push creds.

QUICK_SUITE = {
    "Sapphire": "python -m pytest tests/unit -q -x --timeout=300",
}

TASK_TIMEOUT_S = 45 * 60
TEST_TIMEOUT_S = 30 * 60
SWEEP_INTERVAL_H = 12
POLL_S = 60
MAX_DIFF_LINES = 3000
MAX_AUTO_QUEUED = 2
MAX_AUTO_PER_DAY = 4
MAX_PROPOSED = 12


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "task"


def parse_task(path: Path) -> dict | None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    head, sep, body = raw.partition("\n---\n")
    if not sep:
        return None
    fields: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().lower()] = v.strip()
    repo = fields.get("repo", "")
    goal = body.strip()
    if not repo or not goal:
        return None
    return {"name": path.stem, "repo": Path(repo), "test": fields.get("test", ""), "goal": goal}


def repo_allowed(repo: Path) -> bool:
    try:
        r = repo.resolve()
    except OSError:
        return False
    return any(
        r == a.resolve() or str(r).startswith(str(a.resolve()) + "\\")
        for a in ALLOWLIST if a.exists()
    )


def default_branch(repo: Path) -> str:
    """Return the repo's default branch (main or master)."""
    # Ask the remote what the default branch is.
    for cmd in (
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "origin/HEAD"],
        ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
    ):
        code, out = sh(cmd, timeout=60)
        if code == 0:
            branch = out.strip().split("/")[-1]
            if branch:
                return branch
    # Fall back to local detection.
    for candidate in ("main", "master"):
        code, _ = sh(["git", "-C", str(repo), "rev-parse", "--verify", candidate], timeout=60)
        if code == 0:
            return candidate
    return "main"  # safest modern default


def sh(cmd: list[str] | str, cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, shell=isinstance(cmd, str),
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except Exception as e:  # noqa: BLE001 — worker must never die on one task
        return 125, f"EXEC ERROR: {e}"


def heartbeat(state: str, detail: str = "") -> None:
    HEARTBEAT.write_text(
        json.dumps({"ts": now(), "state": state, "detail": detail[:200]}), encoding="utf-8"
    )


def report(name: str, lines: list[str]) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"{dt.date.today().isoformat()}-{name}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ── v2: guards ────────────────────────────────────────────────────────────────

def diff_lines(repo: Path, base: str | None = None) -> int:
    base = base or default_branch(repo)
    code, out = sh(["git", "-C", str(repo), "diff", "--shortstat", f"{base}...HEAD"], timeout=120)
    if code != 0:
        return 0
    m = re.findall(r"(\d+) (?:insertion|deletion)", out)
    return sum(int(x) for x in m)


def changed_source_files(repo: Path, base: str | None = None) -> list[str]:
    base = base or default_branch(repo)
    code, out = sh(["git", "-C", str(repo), "diff", "--name-only", f"{base}...HEAD"], timeout=120)
    if code != 0:
        return []
    return [f for f in out.splitlines() if f.strip() and not f.startswith(("tests/", "tests\\"))]


def commits_made(repo: Path, base: str | None = None) -> int:
    base = base or default_branch(repo)
    code, out = sh(["git", "-C", str(repo), "rev-list", "--count", f"{base}..HEAD"], timeout=120)
    try:
        return int(out.strip()) if code == 0 else 0
    except ValueError:
        return 0


def make_bundle(repo: Path, name: str, base: str | None = None) -> str:
    base = base or default_branch(repo)
    dest = REPORTS / f"{dt.date.today().isoformat()}-{name}.bundle"
    code, out = sh(["git", "-C", str(repo), "bundle", "create", str(dest), f"{base}..HEAD"], timeout=300)
    return str(dest) if code == 0 else f"bundle failed: {out[-200:]}"


# ── task execution ────────────────────────────────────────────────────────────

def run_task(task: dict) -> bool:
    name, repo, goal, test = task["name"], task["repo"], task["goal"], task["test"]
    lines = [f"# worker task: {name}", f"- started: {now()}", f"- repo: {repo}",
             f"- goal: {goal[:300]}"]

    if not repo_allowed(repo):
        lines += ["", "REJECTED: repo not in ALLOWLIST — nothing executed."]
        report(name, lines)
        return False

    branch = f"worker/{slug(name)}"
    base = default_branch(repo)
    sh(["git", "-C", str(repo), "stash", "--include-untracked"], timeout=120)
    code, _ = sh(["git", "-C", str(repo), "switch", "-C", branch, base], timeout=120)
    lines.append(f"- branch: {branch} from {base} (rc={code})")

    ok = False
    test_out = ""
    for attempt in (1, 2):
        heartbeat("task", f"{name} attempt {attempt}")
        msg = goal if attempt == 1 else (
            goal + "\n\nThe previous attempt failed these tests — fix the failures:\n" + test_out[-3000:]
        )
        # --map-tokens 1024 + manual refresh: the maiden run showed aider's
        # post-commit repo-map refresh on this 8,500-file repo can hang for the
        # full 45-min ceiling. A small, non-refreshing map keeps edits fast.
        code, out = sh(
            [str(AIDER), "--model", MODEL, "--weak-model", WEAK_MODEL,
             "--no-show-model-warnings", "--yes-always", "--no-stream",
             "--map-tokens", "1024", "--map-refresh", "manual",
             "--auto-commits", "--message", msg],
            cwd=repo, timeout=TASK_TIMEOUT_S,
        )
        lines += ["", f"## aider attempt {attempt} (rc={code})", "```", out[-2500:], "```"]

        n_diff = diff_lines(repo)
        if n_diff > MAX_DIFF_LINES:
            lines += [f"", f"RUNAWAY GUARD: diff is {n_diff} lines (> {MAX_DIFF_LINES}) — failing task."]
            break

        if not test:
            ok = code == 0
            break
        code_t, test_out = sh(test, cwd=repo, timeout=TEST_TIMEOUT_S)
        lines += [f"## tests attempt {attempt} (rc={code_t})", "```", test_out[-2000:], "```"]
        if code_t == 0:
            ok = True
            break

    # No-op guard: a "PASS" with zero commits means the agent changed nothing —
    # the task tests passing is then vacuous (the maiden run hit exactly this).
    if ok and commits_made(repo) == 0:
        lines += ["", "NO-OP GUARD: task tests pass but the agent produced no commits — marking FAIL."]
        ok = False

    # Regression gate: source-touching diffs must also pass the repo quick suite.
    if ok:
        touched = changed_source_files(repo)
        gate = QUICK_SUITE.get(repo.name)
        if touched and gate:
            heartbeat("task", f"{name} regression gate")
            code_g, gate_out = sh(gate, cwd=repo, timeout=TEST_TIMEOUT_S)
            tail = "\n".join(gate_out.strip().splitlines()[-5:])
            lines += [f"## regression gate (source files touched: {len(touched)}) rc={code_g}",
                      "```", tail, "```"]
            ok = code_g == 0

    bundle = make_bundle(repo, name)
    lines += ["", f"- bundle: {bundle}", f"- finished: {now()}",
              f"- RESULT: {'PASS' if ok else 'FAIL'}",
              "- commits stay on the worker/* branch; review + push happen from the Mac (`sov worker pull`)."]
    report(name, lines)
    return ok


# ── v2: backlog mining ────────────────────────────────────────────────────────

def auto_budget() -> int:
    """How many auto tasks we may still create today."""
    queued_auto = len(list(QUEUE.glob("auto-*.md")))
    if queued_auto >= MAX_AUTO_QUEUED:
        return 0
    today = dt.date.today().isoformat()
    created_today = len(list(DONE.glob("auto-*.md"))) + len(list(FAILED.glob("auto-*.md")))
    created_today += queued_auto
    # cheap approximation: count today's auto reports instead of parsing timestamps
    created_today = min(created_today, len(list(REPORTS.glob(f"{today}-auto-*.md"))) + queued_auto)
    return max(0, min(MAX_AUTO_QUEUED - queued_auto, MAX_AUTO_PER_DAY - created_today))


def write_task(dirpath: Path, name: str, repo: Path, test: str, goal: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / f"{name}.md"
    f.write_text(f"repo: {repo}\ntest: {test}\n---\n{goal}\n", encoding="utf-8")
    return f


def mine_backlog() -> None:
    """Generate work from repo signals. LOW risk -> queue, MEDIUM -> proposed."""
    budget = auto_budget()
    existing = {p.stem for p in QUEUE.glob("*.md")} | {p.stem for p in PROPOSED.glob("*.md")} \
        | {p.stem for p in DONE.glob("*.md")} | {p.stem for p in FAILED.glob("*.md")}

    for repo in ALLOWLIST:
        if not repo.exists():
            continue
        # LOW: ruff violations, one file per task
        if budget > 0:
            code, out = sh(f"{PYTHON} -m ruff check --output-format=concise .",
                           cwd=repo, timeout=600)
            if code != 0 and out.strip():
                by_file: dict[str, int] = {}
                for line in out.splitlines():
                    fpath = line.split(":", 1)[0].strip()
                    if fpath.endswith(".py"):
                        by_file[fpath] = by_file.get(fpath, 0) + 1
                for fpath, n in sorted(by_file.items(), key=lambda kv: -kv[1])[:budget]:
                    name = f"auto-ruff-{slug(fpath)}"
                    if name in existing:
                        continue
                    write_task(
                        QUEUE, name, repo,
                        QUICK_SUITE.get(repo.name, ""),
                        f"Run `ruff check {fpath}` and fix ALL reported violations in that one file. "
                        f"Behavior-preserving changes only — no refactors beyond what the lint fixes require.",
                    )
                    existing.add(name)
                    budget -= 1

        # MEDIUM: TODO/FIXME mining -> proposals only
        if len(list(PROPOSED.glob("*.md"))) < MAX_PROPOSED:
            code, out = sh(["git", "-C", str(repo), "grep", "-nE", "(TODO|FIXME)[:( ]", "--", "*.py"],
                           timeout=300)
            if code != 0:      # 1 = no matches, 128 = not a git repo; either way no lines
                out = ""
            for line in out.splitlines()[:5]:
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                fpath, lineno, text = parts[0], parts[1], parts[2].strip()[:120]
                name = f"todo-{slug(fpath)}-l{lineno}"
                if name in existing or len(list(PROPOSED.glob("*.md"))) >= MAX_PROPOSED:
                    continue
                write_task(
                    PROPOSED, name, repo, QUICK_SUITE.get(repo.name, ""),
                    f"Resolve the TODO/FIXME at {fpath}:{lineno}: \"{text}\". "
                    f"If resolving it safely is not possible, replace the comment with a short "
                    f"explanation of what is actually needed.",
                )
                existing.add(name)


# ── sweep + main loop ─────────────────────────────────────────────────────────

def sweep() -> None:
    stamp = BASE / "last-sweep.txt"
    if stamp.exists():
        age_h = (time.time() - stamp.stat().st_mtime) / 3600
        if age_h < SWEEP_INTERVAL_H:
            return
    heartbeat("sweep")
    lines = [f"# regression sweep {now()}"]
    for repo in ALLOWLIST:
        gate = QUICK_SUITE.get(repo.name)
        if not repo.exists() or not gate:
            continue
        sh(["git", "-C", str(repo), "switch", "-q", default_branch(repo)], timeout=120)
        code, out = sh(gate, cwd=repo, timeout=TEST_TIMEOUT_S)
        tail = "\n".join(out.strip().splitlines()[-5:])
        lines += [f"## {repo.name} (rc={code})", "```", tail, "```"]
    report("sweep", lines)
    stamp.write_text(now(), encoding="utf-8")
    heartbeat("mining")
    mine_backlog()


def main() -> None:
    for d in (QUEUE, PROPOSED, DONE, FAILED, REPORTS):
        d.mkdir(parents=True, exist_ok=True)
    heartbeat("start")
    while True:
        tasks = sorted(QUEUE.glob("*.md"), key=lambda p: p.stat().st_mtime)
        if tasks:
            path = tasks[0]
            task = parse_task(path)
            if task is None:
                report(path.stem, [f"# malformed task {path.name}",
                                   "missing 'repo:' header or '---' separator"])
                path.rename(FAILED / path.name)
                continue
            ok = run_task(task)
            path.rename((DONE if ok else FAILED) / path.name)
            heartbeat("idle", f"finished {path.stem}: {'PASS' if ok else 'FAIL'}")
        else:
            sweep()
            heartbeat("idle")
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
