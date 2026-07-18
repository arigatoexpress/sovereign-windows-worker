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
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

HOME = Path.home()
BASE = HOME / "agent-worker"
QUEUE = BASE / "queue"
PROPOSED = BASE / "proposed"
DONE = BASE / "done"
FAILED = BASE / "failed"
REPORTS = BASE / "reports"
HEARTBEAT = BASE / "heartbeat.json"
METRICS = BASE / "metrics.json"
BASELINE_CACHE = BASE / "baseline-cache"
INSTANCE_LOCK = BASE / "worker.lock"
START_TIME = time.time()

AIDER = Path(os.environ.get("SOV_WORKER_AIDER", HOME / ".aider-venv" / "Scripts" / "aider.exe"))
PYTHON = Path(os.environ.get("SOV_WORKER_PYTHON", HOME / "AppData" / "Local" / "Programs" / "Python" / "Python313" / "python.exe"))
MODEL = os.environ.get("SOV_WORKER_MODEL", "ollama/qwen3-coder:30b")
WEAK_MODEL = os.environ.get("SOV_WORKER_WEAK_MODEL", "ollama/gemma3:4b")
MAP_TOKENS = os.environ.get("SOV_WORKER_MAP_TOKENS", "512")
MAX_CHAT_HISTORY_TOKENS = os.environ.get("SOV_WORKER_MAX_CHAT_TOKENS", "8192")
EDIT_FORMAT = os.environ.get("SOV_WORKER_EDIT_FORMAT", "diff")
RELEASE = os.environ.get("SOV_WORKER_RELEASE", "source")
BASELINE_CACHE_TTL_S = int(os.environ.get("SOV_WORKER_BASELINE_CACHE_TTL_S", "21600"))

ALLOWLIST = [
    HOME / "Code" / "Sapphire",
    HOME / "Code" / "telemetry-dashboard",
    HOME / "Code" / "claw-code",
]
# NEVER add Project-Go-Forward (THO client prod fence) or any repo with push creds.

QUICK_SUITE = {
    # Collect the complete failure set. With -x, fixing the first baseline
    # failure exposes the next pre-existing failure and falsely looks like a
    # regression.
    "Sapphire": "python -m pytest tests/unit -q --timeout=300",
}

TASK_TIMEOUT_S = 45 * 60
TEST_TIMEOUT_S = 30 * 60
SWEEP_INTERVAL_H = 12
POLL_S = 60
MAX_DIFF_LINES = 3000
MAX_AUTO_QUEUED = 2
MAX_AUTO_PER_DAY = 4
MAX_PROPOSED = 12
THO_SOURCE = "gmail-mark-tho"
THO_ENABLED = os.environ.get("SOV_WORKER_THO_ENABLED", "0") == "1"
THO_MAX_DIFF_LINES = 500
THO_CANONICAL_TESTS = (
    "tests/test_healthz.py",
    "tests/test_api_v1.py",
    "tests/test_document_engine.py",
)

TASK_CHARTER = """Work from the exact failing contract and make the smallest root-cause change.
Preserve production behavior and structured-data formats. Do not merely add diagnostics,
weaken assertions, or redesign unrelated code. The worker will run tests after your edit."""

ANALYSIS_CHARTER = """Read-only evidence review. Ground every claim in the provided files and
cite file:line. Separate observed behavior from inference. Do not call a missing test a
vulnerability or invent implementation behavior; state what cannot be determined when the
implementation is absent. Return the single highest-impact risk and one exact validation step.
Do not edit, create, delete, or commit any file."""


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
    analysis_only = (
        goal.upper().startswith("ANALYSIS ONLY")
        or "do not modify any files" in goal.lower()
    )
    return {
        "name": path.stem,
        "repo": Path(repo),
        "test": fields.get("test", ""),
        "goal": goal,
        "analysis_only": analysis_only,
        "source": fields.get("source", ""),
        "message_id": fields.get("message_id", ""),
        "message_date": fields.get("message_date", ""),
        "base_sha": fields.get("base_sha", ""),
        "base_bundle": fields.get("base_bundle", ""),
    }


def repo_allowed(repo: Path) -> bool:
    try:
        r = repo.resolve()
    except OSError:
        return False
    if not r.exists():
        return False
    return any(
        r == a.resolve() or r.is_relative_to(a.resolve())
        for a in ALLOWLIST if a.exists()
    )


def is_tho_task(task: dict) -> bool:
    """Return whether this is an explicit THO request, never a generic repo task."""
    repo_name = str(task.get("repo", "")).replace("\\", "/").rstrip("/").split("/")[-1]
    return task.get("source") == THO_SOURCE and repo_name == "Project-Go-Forward"


def _tho_bundle_path(task: dict) -> Path:
    incoming = (BASE / "incoming-bundles").resolve()
    supplied = Path(str(task.get("base_bundle", "")))
    return (incoming / supplied).resolve() if not supplied.is_absolute() else supplied.resolve()


def _verify_bundle(bundle: Path) -> tuple[bool, str]:
    """Run git's full bundle verification in a disposable empty repository."""
    workspaces = BASE / "tho-workspaces"
    workspaces.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verify-", dir=workspaces) as temp:
        verifier = Path(temp)
        code, out = sh(["git", "init", "--bare", str(verifier)], timeout=60)
        if code != 0:
            return False, f"could not initialize bundle verifier: {out[-200:]}"
        code, out = sh(["git", "-C", str(verifier), "bundle", "verify", str(bundle)], timeout=120)
        return code == 0, out


def _bundle_origin_main_head(bundle: Path) -> tuple[str | None, str]:
    code, out = sh(
        ["git", "bundle", "list-heads", str(bundle), "refs/remotes/origin/main"],
        timeout=120,
    )
    if code != 0:
        return None, out
    lines = [line.split() for line in out.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != "refs/remotes/origin/main":
        return None, "bundle must advertise exactly refs/remotes/origin/main"
    return lines[0][0], ""


def parse_tho_test_command(command: str) -> list[str]:
    """Constrain THO tests to shell-free, repo-local pytest or npm invocations."""
    command = command.strip()
    if not command:
        raise ValueError("test command is required")
    if re.search(r"[;&|<>`$^\r\n]", command) or re.search(r"%[^%]+%", command):
        raise ValueError("test command contains shell metacharacters or substitution")
    if re.search(r"(?:^|\s)(?:[A-Za-z]:[\\/]|\\\\|//)", command):
        raise ValueError("test command contains an absolute Windows path")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid test command quoting: {exc}") from exc
    if not argv:
        raise ValueError("test command is required")

    executable = argv[0].lower()
    if "/" in executable or "\\" in executable:
        raise ValueError("test executable must be resolved from PATH")
    python_commands = {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}
    pytest_commands = {"pytest", "pytest.exe"}
    npm_commands = {"npm", "npm.cmd"}
    if executable in python_commands:
        if len(argv) < 3 or argv[1:3] != ["-m", "pytest"]:
            raise ValueError("Python THO tests must use 'python -m pytest'")
    elif executable in pytest_commands:
        pass
    elif executable in npm_commands:
        if len(argv) < 2 or argv[1] not in {"test", "run"}:
            raise ValueError("npm THO tests must use 'npm test' or 'npm run test*'")
        if argv[1] == "run" and (len(argv) < 3 or not argv[2].lower().startswith("test")):
            raise ValueError("npm run script must start with 'test'")
    else:
        raise ValueError("test executable must be Python pytest or npm")

    for token in argv[1:]:
        candidate = token.split("=", 1)[-1] if "=" in token else token
        if "://" in candidate:
            raise ValueError("test arguments must not reference external URLs")
        if (PurePosixPath(candidate).is_absolute()
                or PureWindowsPath(candidate).is_absolute()
                or ".." in PurePosixPath(candidate.replace("\\", "/")).parts
                or candidate.startswith("~")):
            raise ValueError("test arguments must use repo-local paths")
    return argv


def validate_tho_task(task: dict) -> tuple[bool, str]:
    """Validate provenance and the contained, complete Git bundle for a THO task."""
    if not is_tho_task(task):
        return False, f"source must be exactly {THO_SOURCE} for Project-Go-Forward"
    for field in ("message_id", "message_date", "test"):
        if not str(task.get(field, "")).strip():
            return False, f"missing required {field}"
    sha = str(task.get("base_sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False, "base_sha must be exactly 40 lowercase hexadecimal characters"
    raw_bundle = str(task.get("base_bundle", "")).strip()
    if not raw_bundle:
        return False, "missing required base_bundle"
    bundle = _tho_bundle_path(task)
    incoming = (BASE / "incoming-bundles").resolve()
    if not bundle.is_relative_to(incoming):
        return False, "base_bundle must resolve beneath incoming-bundles"
    if bundle.suffix.lower() != ".bundle" or not bundle.is_file():
        return False, "base_bundle must be an existing .bundle file"
    try:
        parse_tho_test_command(str(task["test"]))
    except ValueError as exc:
        return False, f"unsafe test command: {exc}"
    verified, output = _verify_bundle(bundle)
    if not verified:
        return False, f"git bundle verify failed: {output[-200:]}"
    advertised_sha, output = _bundle_origin_main_head(bundle)
    if advertised_sha is None:
        return False, output
    if advertised_sha != sha:
        return False, "base_sha must equal the bundle-advertised origin/main head"
    return True, ""


def prepare_tho_workspace(task: dict) -> Path:
    """Clone a verified THO bundle and detach at the exact provenance SHA."""
    valid, reason = validate_tho_task(task)
    if not valid:
        raise ValueError(reason)
    sha = task["base_sha"]
    parent = BASE / "tho-workspaces"
    parent.mkdir(parents=True, exist_ok=True)
    workspace = parent / f"{slug(task['name'])}-{sha[:12]}"
    if workspace.exists():
        raise ValueError(f"THO workspace already exists: {workspace}")
    try:
        workspace.mkdir()
        code, out = sh(["git", "-C", str(workspace), "init"], timeout=120)
        if code != 0:
            raise ValueError(f"workspace init failed: {out[-300:]}")
        code, out = sh(
            ["git", "-C", str(workspace), "fetch", str(_tho_bundle_path(task)),
             "refs/remotes/origin/main:refs/remotes/origin/main"], timeout=300,
        )
        if code != 0:
            raise ValueError(f"bundle fetch failed: {out[-300:]}")
        code, out = sh(["git", "-C", str(workspace), "checkout", "--detach", sha], timeout=120)
        if code != 0:
            raise ValueError(f"base_sha is absent or stale for bundle: {out[-300:]}")
        code, head = sh(["git", "-C", str(workspace), "rev-parse", "HEAD"], timeout=60)
        if code != 0 or head.strip() != sha:
            raise ValueError("workspace HEAD does not equal the supplied base_sha")
        return workspace
    except Exception as original:
        try:
            shutil.rmtree(workspace)
        except Exception as cleanup_error:
            raise RuntimeError(
                f"workspace preparation failed ({original}); cleanup failed for "
                f"{workspace}: {cleanup_error}"
            ) from cleanup_error
        if workspace.exists():
            raise RuntimeError(
                f"workspace preparation failed ({original}); cleanup left {workspace} present"
            ) from original
        raise


def validate_tho_changed_paths(repo: Path, base: str) -> tuple[bool, list[str]]:
    """Reject client documents, automation, env files, and credential-like paths."""
    code, out = sh(
        ["git", "-C", str(repo), "diff", "--no-renames", "--name-only", base], timeout=120,
    )
    if code != 0:
        return False, ["<unable to enumerate changed paths>"]
    code_u, untracked = sh(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"], timeout=120,
    )
    if code_u != 0:
        return False, ["<unable to enumerate untracked paths>"]
    rejected: list[str] = []
    changed = dict.fromkeys(out.splitlines() + untracked.splitlines())
    for raw in filter(None, (line.strip() for line in changed)):
        normalized = raw.replace("\\", "/")
        lower = normalized.lower()
        parts = Path(normalized).parts
        unsafe_shape = Path(normalized).is_absolute() or ".." in parts
        filename = Path(lower).name
        prohibited = (
            filename == ".env" or filename.startswith(".env.")
            or lower.startswith("tho_documents/")
            or lower.startswith(".github/workflows/")
            or any(token in lower for token in ("credential", "service-account", "id_rsa", "id_ed25519"))
            or Path(lower).suffix in {".pem", ".key", ".p12", ".pfx"}
        )
        if unsafe_shape or prohibited:
            rejected.append(raw)
    return not rejected, rejected


def _tho_diff_lines(repo: Path, base: str) -> int:
    """Count committed, working-tree, and untracked THO changes from the exact SHA."""
    code, out = sh(["git", "-C", str(repo), "diff", "--numstat", base], timeout=120)
    if code != 0:
        return THO_MAX_DIFF_LINES + 1
    total = 0
    for line in out.splitlines():
        columns = line.split("\t", 2)
        for count in columns[:2]:
            total += int(count) if count.isdigit() else THO_MAX_DIFF_LINES + 1
    code, untracked = sh(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"], timeout=120,
    )
    if code != 0:
        return THO_MAX_DIFF_LINES + 1
    for relative in untracked.splitlines():
        try:
            total += len((repo / relative).read_bytes().splitlines())
        except OSError:
            return THO_MAX_DIFF_LINES + 1
    return total


def default_branch(repo: Path) -> str | None:
    """Return the repo's default branch, or None if the repo has no commits."""
    if not repo.exists():
        return None
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
    # Ask git what the default branch should be for new repos.
    code, out = sh(["git", "config", "--get", "init.defaultBranch"], timeout=60)
    if code == 0 and out.strip():
        candidate = out.strip()
        c, _ = sh(["git", "-C", str(repo), "rev-parse", "--verify", candidate], timeout=60)
        if c == 0:
            return candidate
    # Fall back to local detection.
    for candidate in ("main", "master"):
        code, _ = sh(["git", "-C", str(repo), "rev-parse", "--verify", candidate], timeout=60)
        if code == 0:
            return candidate
    # Repo may be empty (no commits yet).
    code, out = sh(["git", "-C", str(repo), "branch", "--list"], timeout=60)
    if code == 0:
        branches = [b.strip().lstrip("* ") for b in out.strip().splitlines()]
        if len(branches) == 1:
            return branches[0]
    return None


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


def goal_files(goal: str, repo: Path) -> list[str]:
    """Return existing repo-relative files explicitly named in a task goal."""
    seen: set[str] = set()
    files: list[str] = []
    for raw in re.findall(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+)", goal):
        relative = raw.replace("\\", "/").split("::", 1)[0].rstrip(".,:;)")
        if relative in seen or not (repo / relative).is_file():
            continue
        seen.add(relative)
        files.append(relative)
        if len(files) == 8:
            break
    return files


def aider_command(message: str, files: list[str] | None = None) -> list[str]:
    """Build the deterministic, non-interactive local Aider invocation."""
    command = [
        str(AIDER),
        "--model", MODEL,
        "--weak-model", WEAK_MODEL,
        "--edit-format", EDIT_FORMAT,
        "--no-show-model-warnings",
        "--yes-always",
        "--no-stream",
        "--no-pretty",
        "--no-fancy-input",
        "--no-notifications",
        "--no-check-update",
        "--no-gitignore",
        "--map-tokens", MAP_TOKENS,
        "--map-multiplier-no-files", "1",
        "--map-refresh", "manual",
        "--max-chat-history-tokens", MAX_CHAT_HISTORY_TOKENS,
        "--auto-commits",
    ]
    command.extend(files or [])
    command.extend(["--message", message])
    return command


def task_message(goal: str, failure_output: str = "") -> str:
    analysis_only = (
        goal.upper().startswith("ANALYSIS ONLY")
        or "do not modify any files" in goal.lower()
    )
    charter = ANALYSIS_CHARTER if analysis_only else TASK_CHARTER
    message = f"{charter}\n\nTask:\n{goal}"
    if failure_output:
        message += "\n\nThe previous attempt failed these tests; fix the exact failures:\n"
        message += failure_output[-3000:]
    return message


_heartbeat_state: dict[str, object] = {"state": "init", "detail": ""}


def heartbeat(state: str, detail: str = "") -> None:
    _heartbeat_state["state"] = state
    _heartbeat_state["detail"] = detail[:200]
    payload = {
        "ts": now(),
        "state": state,
        "release": RELEASE,
        "model": MODEL,
        "edit_format": EDIT_FORMAT,
        "detail": detail[:200],
        "uptime_s": int(time.time() - START_TIME),
    }
    # Add extra context if we are in a task.
    payload.update(_heartbeat_state)
    tmp = HEARTBEAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(HEARTBEAT)


def load_metrics() -> dict:
    if METRICS.exists():
        try:
            return json.loads(METRICS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"tasks": 0, "pass": 0, "fail": 0, "timeouts": 0, "last_sweep": ""}


def save_metrics(m: dict) -> None:
    tmp = METRICS.with_suffix(".tmp")
    tmp.write_text(json.dumps(m), encoding="utf-8")
    tmp.replace(METRICS)


def update_metrics(
    *, increment: tuple[str, ...] = (), values: dict | None = None
) -> dict:
    """Reload, mutate, and persist metrics so live operator resets are honored."""
    metrics = load_metrics()
    for key in increment:
        metrics[key] = int(metrics.get(key, 0)) + 1
    if values:
        metrics.update(values)
    save_metrics(metrics)
    return metrics


def report(name: str, lines: list[str]) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = REPORTS / f"{dt.date.today().isoformat()}-{name}-{ts}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def archive_task(path: Path, dest_dir: Path) -> Path:
    """Move a task file into dest_dir, suffixing the name if a file already exists.

    Path.rename() raises FileExistsError on Windows when the destination exists,
    which crashed the main loop when a task was re-queued with the same stem.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    counter = 1
    stem, suffix = path.stem, path.suffix
    while dest.exists():
        dest = dest_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    path.rename(dest)
    return dest


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


def has_worktree_changes(repo: Path) -> bool:
    """Return True for tracked or untracked task changes left outside commits."""
    code, out = worktree_status(repo)
    return code != 0 or bool(out.strip())


def worktree_status(repo: Path) -> tuple[int, str]:
    """Return a stable, fully expanded worktree snapshot for delta checks."""
    return sh(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        timeout=120,
    )


def acquire_instance_lock(path: Path = INSTANCE_LOCK):
    """Acquire an OS-released singleton lock, returning its open handle.

    Scheduled Task restarts can overlap wrappers. The lock is held by the
    Python process and released by the OS even after a crash or forced stop.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii"))
    handle.flush()
    return handle


def release_instance_lock(handle) -> None:
    if handle is None or handle.closed:
        return
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


_PYTEST_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR)\s+([^\s]+)", re.MULTILINE)


def pytest_failure_ids(output: str) -> set[str]:
    """Extract stable pytest node IDs from the short test summary."""
    return {match.group(1) for match in _PYTEST_FAILURE_RE.finditer(output)}


def regression_verdict(
    baseline_code: int,
    baseline_output: str,
    candidate_code: int,
    candidate_output: str,
) -> tuple[bool, str, set[str]]:
    """Accept a red candidate only when all failures already exist on the base."""
    if candidate_code == 0:
        return True, "candidate quick suite is green", set()
    if candidate_code in (124, 125):
        label = "timed out" if candidate_code == 124 else "could not execute"
        return False, f"candidate quick suite {label}", set()

    candidate_failures = pytest_failure_ids(candidate_output)
    baseline_failures = pytest_failure_ids(baseline_output)
    if baseline_code == 0:
        return False, "candidate introduced failures against a green baseline", candidate_failures
    if baseline_code in (124, 125):
        return False, "baseline quick suite was inconclusive; failing closed", candidate_failures
    if not baseline_failures or not candidate_failures:
        return False, "quick-suite failure could not be compared safely", candidate_failures

    new_failures = candidate_failures - baseline_failures
    if new_failures:
        return False, f"candidate introduced {len(new_failures)} new failure(s)", new_failures
    return True, "candidate failures are baseline-equivalent", set()


def _baseline_cache_path(repo: Path, base_sha: str, command: str) -> Path:
    material = f"{repo.resolve()}\0{base_sha}\0{command}".encode()
    return BASELINE_CACHE / f"{hashlib.sha256(material).hexdigest()}.json"


def baseline_gate(repo: Path, base: str, command: str) -> tuple[int, str, str]:
    """Return a complete baseline result, cached by exact HEAD and command."""
    code_sha, base_sha = sh(["git", "-C", str(repo), "rev-parse", base], timeout=60)
    if code_sha != 0 or not base_sha.strip():
        return 125, f"could not resolve baseline {base}: {base_sha}", "error"
    cache_path = _baseline_cache_path(repo, base_sha.strip(), command)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if time.time() - float(cached["created_at"]) <= BASELINE_CACHE_TTL_S:
            return int(cached["code"]), str(cached["output"]), "hit"
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass

    code, output = sh(command, cwd=repo, timeout=TEST_TIMEOUT_S)
    if code not in (124, 125):
        BASELINE_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"created_at": time.time(), "code": code, "output": output}),
            encoding="utf-8",
        )
        tmp.replace(cache_path)
    return code, output, "miss"


def make_bundle(repo: Path, name: str, base: str | None = None) -> str:
    base = base or default_branch(repo)
    if not base or commits_made(repo, base) == 0:
        return "no commits to bundle"
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = REPORTS / f"{dt.date.today().isoformat()}-{name}-{ts}.bundle"
    code, out = sh(["git", "-C", str(repo), "bundle", "create", str(dest), f"{base}..HEAD"], timeout=300)
    if code != 0:
        return f"bundle failed: {out[-200:]}"
    code_v, out_v = sh(["git", "-C", str(repo), "bundle", "verify", str(dest)], timeout=120)
    if code_v != 0:
        dest.unlink(missing_ok=True)
        return f"bundle verify failed: {out_v[-200:]}"
    return str(dest)


# ── task execution ────────────────────────────────────────────────────────────

def _cleanup_repo(repo: Path, base: str | None, branch: str, stash_ref: str | None) -> None:
    """Restore repo to base branch and pop stash. Best-effort; never raises."""
    try:
        sh(["git", "-C", str(repo), "switch", "-q", base or "main"], timeout=120)
    except Exception:
        pass
    if branch:
        try:
            # Delete the worker branch if it exists so stale branches don't accumulate.
            sh(["git", "-C", str(repo), "branch", "-D", branch], timeout=120)
        except Exception:
            pass
    if stash_ref:
        try:
            sh(["git", "-C", str(repo), "stash", "pop", stash_ref], timeout=120)
        except Exception:
            pass


def _tho_head(repo: Path) -> tuple[str | None, str]:
    code, out = sh(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=60)
    return (out.strip(), "") if code == 0 else (None, out[-300:])


def _tho_clean_tree(repo: Path) -> tuple[bool, str]:
    code, out = sh(["git", "-C", str(repo), "status", "--porcelain"], timeout=60)
    if code != 0:
        return False, f"git status failed: {out[-300:]}"
    if out.strip():
        return False, out.strip()[:500]
    return True, ""


def _tho_final_artifact_guard(repo: Path, base: str, expected_head: str) -> tuple[bool, str]:
    head, error = _tho_head(repo)
    if head is None:
        return False, f"could not read final HEAD: {error}"
    if head != expected_head:
        return False, f"final HEAD changed from tested {expected_head} to {head}"
    clean, detail = _tho_clean_tree(repo)
    if not clean:
        return False, f"final working tree is not clean: {detail}"
    n_diff = _tho_diff_lines(repo, base)
    if n_diff > THO_MAX_DIFF_LINES:
        return False, f"final diff is {n_diff} lines (> {THO_MAX_DIFF_LINES})"
    paths_ok, rejected = validate_tho_changed_paths(repo, base)
    if not paths_ok:
        return False, "final prohibited paths: " + ", ".join(rejected)
    return True, ""


def _run_tho_task(task: dict) -> bool:
    """Execute an explicit THO request only inside its disposable bundle clone."""
    name, goal, test = task["name"], task["goal"], task["test"]
    lines = [f"# worker task: {name}", f"- started: {now()}",
             f"- repo: {task['repo']}", f"- source: {task.get('source', '')}",
             f"- message-id: {task.get('message_id', '')}", f"- goal: {goal[:300]}"]
    valid, reason = validate_tho_task(task)
    if not valid:
        lines += ["", f"REJECTED: invalid THO provenance — {reason}"]
        report(name, lines)
        return False
    test_argv = parse_tho_test_command(test)

    workspace: Path | None = None
    branch = f"worker/{slug(name)}"
    base = task["base_sha"]
    ok = False
    test_out = ""
    tested_head: str | None = None
    bundle = "not created: task failed safety gates"
    try:
        workspace = prepare_tho_workspace(task)
        _heartbeat_state.update(task=name, repo=str(workspace), branch=branch)
        code, out = sh(["git", "-C", str(workspace), "switch", "-C", branch, base], timeout=120)
        if code != 0:
            raise RuntimeError(f"could not create worker branch: {out[-300:]}")
        lines.append(f"- isolated workspace: {workspace}")
        lines.append(f"- branch: {branch} from exact SHA {base}")

        for attempt in (1, 2):
            heartbeat("task", f"{name} THO attempt {attempt}")
            message = goal if attempt == 1 else (
                goal + "\n\nThe previous attempt failed these tests — fix the failures:\n"
                + test_out[-3000:]
            )
            code, out = sh(
                aider_command(message, goal_files(goal, workspace)),
                cwd=workspace, timeout=TASK_TIMEOUT_S,
            )
            lines += ["", f"## aider attempt {attempt} (rc={code})", "```", out[-2500:], "```"]

            n_diff = _tho_diff_lines(workspace, base)
            if n_diff > THO_MAX_DIFF_LINES:
                lines += ["", f"THO RUNAWAY GUARD: diff is {n_diff} lines (> {THO_MAX_DIFF_LINES})."]
                break
            paths_ok, rejected = validate_tho_changed_paths(workspace, base)
            if not paths_ok:
                lines += ["", "THO PATH GUARD rejected: " + ", ".join(rejected)]
                break
            clean, detail = _tho_clean_tree(workspace)
            if not clean:
                lines += ["", f"THO CLEAN-TREE GUARD before task tests: {detail}"]
                break
            expected_head, head_error = _tho_head(workspace)
            if expected_head is None:
                lines += ["", f"THO HEAD GUARD before task tests: {head_error}"]
                break
            code_t, test_out = sh(test_argv, cwd=workspace, timeout=TEST_TIMEOUT_S)
            lines += [f"## tests attempt {attempt} (rc={code_t})", "```", test_out[-2000:], "```"]
            after_head, head_error = _tho_head(workspace)
            if after_head is None or after_head != expected_head:
                lines += ["", "THO TEST ARTIFACT GUARD: task tests changed HEAD "
                          f"from {expected_head} to {after_head or head_error}."]
                break
            clean, detail = _tho_clean_tree(workspace)
            if not clean:
                lines += ["", f"THO TEST ARTIFACT GUARD: task tests left changes: {detail}"]
                break
            if code_t == 0:
                ok = True
                tested_head = expected_head
                break

        if ok and commits_made(workspace, base) == 0:
            lines += ["", "NO-OP GUARD: task tests pass but the agent produced no commits — marking FAIL."]
            ok = False

        if ok and changed_source_files(workspace, base):
            heartbeat("task", f"{name} THO canonical regression gate")
            canonical_argv = [str(PYTHON), "-m", "pytest", *THO_CANONICAL_TESTS, "-q"]
            expected_head = tested_head
            code_g, gate_out = sh(canonical_argv, cwd=workspace, timeout=TEST_TIMEOUT_S)
            tail = "\n".join(gate_out.strip().splitlines()[-5:])
            lines += [f"## THO canonical regression gate (rc={code_g})", "```", tail, "```"]
            after_head, head_error = _tho_head(workspace)
            if after_head is None or after_head != expected_head:
                lines += ["", "THO CANONICAL ARTIFACT GUARD: canonical suite changed HEAD "
                          f"from {expected_head} to {after_head or head_error}."]
                ok = False
            else:
                clean, detail = _tho_clean_tree(workspace)
                if not clean:
                    lines += ["", f"THO CANONICAL ARTIFACT GUARD: canonical suite left changes: {detail}"]
                    ok = False
                else:
                    ok = code_g == 0

        if ok:
            final_ok, reason = _tho_final_artifact_guard(workspace, base, tested_head or "")
            if not final_ok:
                lines += ["", f"THO FINAL ARTIFACT GUARD: {reason}"]
                ok = False

        if ok:
            REPORTS.mkdir(parents=True, exist_ok=True)
            bundle = make_bundle(workspace, name, base)
            if not Path(bundle).is_file():
                ok = False
    except Exception as exc:
        lines += ["", f"CRASH: {type(exc).__name__}: {exc}"]
        ok = False
    finally:
        if workspace is not None:
            try:
                shutil.rmtree(workspace)
            except Exception as cleanup_error:
                ok = False
                lines += ["", f"THO CLEANUP FAILED for {workspace}: "
                          f"{type(cleanup_error).__name__}: {cleanup_error}"]
            if workspace.exists():
                ok = False
                lines += ["", f"THO CLEANUP FAILED: workspace still exists: {workspace}"]
        for key in ("task", "repo", "branch"):
            _heartbeat_state.pop(key, None)
    lines += ["", f"- bundle: {bundle}", f"- finished: {now()}",
              f"- RESULT: {'PASS' if ok else 'FAIL'}",
              "- Mac review is required; the worker never pushes, merges, deploys, or sends replies."]
    report(name, lines)
    return ok


def run_task(task: dict) -> bool:
    if is_tho_task(task):
        return _run_tho_task(task)
    name, repo, goal, test = task["name"], task["repo"], task["goal"], task["test"]
    analysis_only = task.get("analysis_only", False)
    lines = [f"# worker task: {name}", f"- started: {now()}", f"- repo: {repo}",
             f"- goal: {goal[:300]}"]

    if not repo_allowed(repo):
        lines += ["", "REJECTED: repo not in ALLOWLIST or path does not exist — nothing executed."]
        report(name, lines)
        return False

    base = default_branch(repo)
    if base is None:
        lines += ["", "REJECTED: repo has no default branch / is empty — nothing executed."]
        report(name, lines)
        return False

    branch = f"worker/{slug(name)}"
    stash_ref: str | None = None
    ok = False
    test_out = ""
    gate = QUICK_SUITE.get(repo.name)
    baseline: tuple[int, str] | None = None
    analysis_status_before: tuple[int, str] | None = None

    _heartbeat_state["task"] = name
    _heartbeat_state["repo"] = str(repo)
    _heartbeat_state["branch"] = branch

    try:
        # Stash pre-existing dirty state and record the stash ref.
        code, stash_out = sh(["git", "-C", str(repo), "stash", "push", "--include-untracked",
                              "-m", f"worker pre-task {name}"], timeout=120)
        if code == 0 and "Saved working directory" in stash_out:
            # Most recent stash is at index 0.
            stash_ref = "0"

        if gate and not analysis_only:
            heartbeat("task", f"{name} baseline regression gate")
            code_b, out_b, cache_status = baseline_gate(repo, base, gate)
            baseline = (code_b, out_b)
            tail = "\n".join(out_b.strip().splitlines()[-5:])
            lines += [
                f"## baseline regression gate (rc={code_b}, cache={cache_status})",
                "```", tail, "```",
            ]

        code, _ = sh(["git", "-C", str(repo), "switch", "-C", branch, base], timeout=120)
        lines.append(f"- branch: {branch} from {base} (rc={code})")
        if analysis_only:
            analysis_status_before = worktree_status(repo)

        for attempt in (1, 2):
            heartbeat("task", f"{name} attempt {attempt}")
            msg = task_message(goal, test_out if attempt == 2 else "")
            code, out = sh(
                aider_command(msg, goal_files(goal, repo)),
                cwd=repo, timeout=TASK_TIMEOUT_S,
            )
            lines += ["", f"## aider attempt {attempt} (rc={code})", "```", out[-2500:], "```"]

            n_diff = diff_lines(repo, base)
            if n_diff > MAX_DIFF_LINES:
                lines += ["", f"RUNAWAY GUARD: diff is {n_diff} lines (> {MAX_DIFF_LINES}) — failing task."]
                break

            if not test:
                ok = code == 0
                break
            code_t, test_out = sh(test, cwd=repo, timeout=TEST_TIMEOUT_S)
            lines += [f"## tests attempt {attempt} (rc={code_t})", "```", test_out[-2000:], "```"]
            if code_t == 0:
                ok = True
                break

        task_commits = commits_made(repo, base)
        # Analysis means read-only: fail closed if the model changed or committed
        # anything. Regular build tasks must have at least one commit.
        analysis_status_after = worktree_status(repo) if analysis_only else None
        analysis_changed = (
            analysis_status_before is None
            or analysis_status_after is None
            or analysis_status_before[0] != 0
            or analysis_status_after[0] != 0
            or analysis_status_before[1] != analysis_status_after[1]
        )
        if ok and analysis_only and (task_commits > 0 or analysis_changed):
            lines += ["", "ANALYSIS-ONLY GUARD: the agent changed files — marking FAIL."]
            ok = False
        elif ok and not analysis_only and task_commits == 0:
            lines += ["", "NO-OP GUARD: task tests pass but the agent produced no commits — marking FAIL."]
            ok = False

        # Regression gate: source-touching diffs must also pass the repo quick suite.
        if ok:
            touched = changed_source_files(repo, base)
            if touched and gate:
                heartbeat("task", f"{name} regression gate")
                code_g, gate_out = sh(gate, cwd=repo, timeout=TEST_TIMEOUT_S)
                tail = "\n".join(gate_out.strip().splitlines()[-5:])
                lines += [f"## regression gate (source files touched: {len(touched)}) rc={code_g}",
                          "```", tail, "```"]
                if baseline is None:
                    ok = code_g == 0
                    reason = "candidate quick suite is green" if ok else "baseline unavailable; failing closed"
                    new_failures: set[str] = set()
                else:
                    ok, reason, new_failures = regression_verdict(
                        baseline[0], baseline[1], code_g, gate_out,
                    )
                lines.append(f"- regression verdict: {reason}")
                if new_failures:
                    lines.append("- new failures: " + ", ".join(sorted(new_failures)))

        bundle = make_bundle(repo, name, base)
        lines += ["", f"- bundle: {bundle}", f"- finished: {now()}",
                  f"- RESULT: {'PASS' if ok else 'FAIL'}",
                  "- commits stay on the worker/* branch; review + push happen from the Mac (`sov worker pull`)."]
    except Exception as e:
        lines += ["", f"CRASH: {type(e).__name__}: {e}"]
        ok = False
    finally:
        _cleanup_repo(repo, base, branch, stash_ref)
        for key in ("task", "repo", "branch"):
            _heartbeat_state.pop(key, None)

    report(name, lines)
    return ok


# ── v2: backlog mining ────────────────────────────────────────────────────────

def auto_budget() -> int:
    """How many auto tasks we may still create today."""
    queued_auto = len(list(QUEUE.glob("auto-*.md")))
    if queued_auto >= MAX_AUTO_QUEUED:
        return 0
    today = dt.date.today().isoformat()
    reports_today = len(list(REPORTS.glob(f"{today}-auto-*.md")))
    created_today = reports_today + queued_auto
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


def execute_task(task: dict) -> bool:
    """Apply deployment feature gates before entering a task lane."""
    if is_tho_task(task) and not THO_ENABLED:
        report(task["name"], [
            f"# worker task: {task['name']}",
            "REJECTED: guarded THO lane is disabled on this worker.",
            "- RESULT: FAIL",
        ])
        return False
    return run_task(task)


def main() -> None:
    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        print("another worker instance holds the singleton lock; exiting")
        return
    for d in (QUEUE, PROPOSED, DONE, FAILED, REPORTS):
        d.mkdir(parents=True, exist_ok=True)
    heartbeat("start")
    while True:
        try:
            tasks = sorted(QUEUE.glob("*.md"), key=lambda p: p.stat().st_mtime)
            if tasks:
                path = tasks[0]
                task = parse_task(path)
                if task is None:
                    report(path.stem, [f"# malformed task {path.name}",
                                       "missing 'repo:' header or '---' separator"])
                    archive_task(path, FAILED)
                    continue
                ok = execute_task(task)
                update_metrics(increment=("tasks", "pass" if ok else "fail"))
                archive_task(path, DONE if ok else FAILED)
                heartbeat("idle", f"finished {path.stem}: {'PASS' if ok else 'FAIL'}")
            else:
                sweep()
                update_metrics(values={"last_sweep": now()})
                heartbeat("idle")
                time.sleep(POLL_S)
        except subprocess.TimeoutExpired:
            # Count aider timeouts via the sh() return code; this catches unexpected loops.
            update_metrics(increment=("timeouts",))
            heartbeat("error", "timeout in main loop")
            time.sleep(POLL_S)
        except Exception as e:
            import traceback
            crash = traceback.format_exc()
            report("crash", [f"# worker crash {now()}", "```", crash[-4000:], "```"])
            heartbeat("crash", f"{type(e).__name__}: {e}")
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
