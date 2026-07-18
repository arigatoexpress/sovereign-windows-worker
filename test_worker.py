"""Unit tests for the sovereign windows worker (pure functions only)."""

import sys
import subprocess as sp
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import worker


def test_aider_command_is_bounded_and_noninteractive():
    cmd = worker.aider_command("fix the test")
    assert cmd[0] == str(worker.AIDER)
    assert cmd[cmd.index("--model") + 1] == worker.MODEL
    assert cmd[cmd.index("--map-tokens") + 1] == "512"
    assert cmd[cmd.index("--max-chat-history-tokens") + 1] == "8192"
    assert "--no-fancy-input" in cmd
    assert "--no-gitignore" in cmd
    assert cmd[-2:] == ["--message", "fix the test"]


def test_sapphire_regression_gate_collects_all_failures():
    command = worker.QUICK_SUITE["Sapphire"]
    assert "tests/unit" in command
    assert "-x" not in command.split()


def _git(repo, *args):
    return sp.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_git_repo(tmp_path):
    repo = tmp_path / "Project-Go-Forward"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    (repo / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")
    return repo


def _make_tho_bundle(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    incoming = tmp_path / "agent-worker" / "incoming-bundles"
    incoming.mkdir(parents=True)
    bundle = incoming / "tho.bundle"
    _git(repo, "bundle", "create", str(bundle), "refs/remotes/origin/main")
    monkeypatch.setattr(worker, "BASE", tmp_path / "agent-worker")
    return repo, base, bundle


def _tho_task(bundle, base, **overrides):
    task = {
        "name": "mark-request",
        "repo": Path(r"C:\Users\aribs\Code\Project-Go-Forward"),
        "test": "python -m pytest tests/test_request.py -q",
        "goal": "Implement the normalized request.",
        "analysis_only": False,
        "source": "gmail-mark-tho",
        "message_id": "18f-mark-message",
        "message_date": "2026-07-13",
        "base_sha": base,
        "base_bundle": str(bundle),
    }
    task.update(overrides)
    return task


def test_parse_task_roundtrip(tmp_path):
    f = tmp_path / "demo.md"
    f.write_text("repo: C:\\Users\\aribs\\Code\\Sapphire\ntest: pytest -q\n---\nAdd a test for X.\n")
    t = worker.parse_task(f)
    assert t["repo"] == Path("C:\\Users\\aribs\\Code\\Sapphire")
    assert t["test"] == "pytest -q"
    assert t["goal"] == "Add a test for X."


def test_parse_task_malformed_returns_none(tmp_path):
    f = tmp_path / "bad.md"
    f.write_text("no separator here")
    assert worker.parse_task(f) is None


def test_parse_task_missing_repo_returns_none(tmp_path):
    f = tmp_path / "norepo.md"
    f.write_text("test: pytest\n---\ngoal text\n")
    assert worker.parse_task(f) is None


def test_allowlist_rejects_outside_repo():
    assert not worker.repo_allowed(Path("C:\\Users\\aribs\\Code\\Project-Go-Forward"))
    assert not worker.repo_allowed(Path("C:\\Windows\\System32"))


def test_allowlist_accepts_sapphire():
    p = Path("C:\\Users\\aribs\\Code\\Sapphire")
    if p.exists():
        assert worker.repo_allowed(p)


def test_slug():
    assert worker.slug("Fix the Thing!") == "fix-the-thing"
    assert len(worker.slug("x" * 200)) <= 48


def test_auto_budget_respects_queue_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "QUEUE", tmp_path / "q")
    monkeypatch.setattr(worker, "DONE", tmp_path / "d")
    monkeypatch.setattr(worker, "FAILED", tmp_path / "f")
    monkeypatch.setattr(worker, "REPORTS", tmp_path / "r")
    for d in (worker.QUEUE, worker.DONE, worker.FAILED, worker.REPORTS):
        d.mkdir()
    (worker.QUEUE / "auto-one.md").write_text("x")
    (worker.QUEUE / "auto-two.md").write_text("x")
    assert worker.auto_budget() == 0


def test_write_task_roundtrips_through_parse(tmp_path):
    f = worker.write_task(tmp_path, "auto-ruff-x", Path("C:\\Users\\aribs\\Code\\Sapphire"),
                          "pytest -q", "Fix lint.")
    t = worker.parse_task(f)
    assert t["repo"] == Path("C:\\Users\\aribs\\Code\\Sapphire")
    assert t["goal"] == "Fix lint."


def test_commits_made_zero_on_bad_repo(tmp_path):
    assert worker.commits_made(tmp_path) == 0


def test_default_branch_detects_main(tmp_path):
    import subprocess as sp
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    (repo / "file.txt").write_text("hello")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    assert worker.default_branch(repo) == "main"


def test_default_branch_falls_back_to_master(tmp_path):
    import subprocess as sp
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-b", "master"], cwd=repo, check=True)
    (repo / "file.txt").write_text("hello")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    assert worker.default_branch(repo) == "master"


def test_default_branch_returns_none_for_empty_repo(tmp_path):
    import subprocess as sp
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    sp.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    assert worker.default_branch(repo) is None


def test_repo_allowed_rejects_nonexistent_path():
    assert not worker.repo_allowed(Path("C:\\Users\\aribs\\Code\\SapphireDoesNotExist"))


def test_repo_allowed_rejects_path_traversal_prefix():
    # A path that starts with the allowlisted prefix but is not inside it.
    assert not worker.repo_allowed(Path("C:\\Users\\aribs\\Code\\SapphireBackup"))


def test_report_names_include_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "REPORTS", tmp_path / "reports")
    out = worker.report("demo", ["line 1"])
    assert out.name.startswith("2")
    assert "demo" in out.name


def test_update_metrics_honors_external_reset(tmp_path, monkeypatch):
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setattr(worker, "METRICS", metrics_path)
    worker.save_metrics(
        {"tasks": 47, "pass": 1, "fail": 46, "timeouts": 0, "last_sweep": "old"}
    )

    # An operator resets counters while the worker process remains alive.
    worker.save_metrics(
        {"tasks": 0, "pass": 0, "fail": 0, "timeouts": 0, "last_sweep": ""}
    )
    updated = worker.update_metrics(increment=("tasks", "pass"))

    assert updated == {
        "tasks": 1,
        "pass": 1,
        "fail": 0,
        "timeouts": 0,
        "last_sweep": "",
    }
    assert worker.load_metrics() == updated


def test_archive_task_moves_file(tmp_path):
    src = tmp_path / "queue" / "task.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x")
    dest = worker.archive_task(src, tmp_path / "done")
    assert dest.name == "task.md"
    assert not src.exists()
    assert dest.exists()


def test_archive_task_avoids_name_collision(tmp_path):
    src = tmp_path / "queue" / "task.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x")
    done_dir = tmp_path / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "task.md").write_text("old")
    dest = worker.archive_task(src, done_dir)
    assert dest.name == "task-1.md"
    assert not src.exists()
    assert dest.exists()


def test_make_bundle_skips_empty_repo(tmp_path, monkeypatch):
    import subprocess as sp
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    (repo / "file.txt").write_text("hello")
    sp.run(["git", "add", "."], cwd=repo, check=True)
    sp.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    monkeypatch.setattr(worker, "REPORTS", tmp_path / "reports")
    result = worker.make_bundle(repo, "empty-task")
    assert "no commits" in result


def test_sh_returns_timeout_code(tmp_path):
    code, out = worker.sh(["sleep", "2"], timeout=1)
    assert code == 124
    assert "TIMEOUT" in out


def test_parse_task_detects_analysis_only_prefix(tmp_path):
    f = tmp_path / "analysis.md"
    f.write_text("repo: C:\\Users\\aribs\\Code\\Sapphire\ntest: \n---\nANALYSIS ONLY: review this.\n")
    t = worker.parse_task(f)
    assert t["analysis_only"] is True


def test_parse_task_detects_analysis_only_phrase(tmp_path):
    f = tmp_path / "analysis.md"
    f.write_text("repo: C:\\Users\\aribs\\Code\\Sapphire\ntest: \n---\nPlease investigate but do not modify any files.\n")
    t = worker.parse_task(f)
    assert t["analysis_only"] is True


def test_parse_task_regular_not_analysis_only(tmp_path):
    f = tmp_path / "regular.md"
    f.write_text("repo: C:\\Users\\aribs\\Code\\Sapphire\ntest: \n---\nFix the bug.\n")
    t = worker.parse_task(f)
    assert t["analysis_only"] is False


def _make_run_task_mocks(monkeypatch, tmp_path):
    """Stub the heavy collaborators so run_task can be unit-tested in isolation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(worker, "repo_allowed", lambda p: True)
    monkeypatch.setattr(worker, "default_branch", lambda p: "main")
    monkeypatch.setattr(worker, "diff_lines", lambda p, base=None: 0)
    monkeypatch.setattr(worker, "commits_made", lambda p, base=None: 0)
    monkeypatch.setattr(worker, "changed_source_files", lambda p, base=None: [])
    monkeypatch.setattr(worker, "make_bundle", lambda p, name, base=None: "no commits to bundle")
    monkeypatch.setattr(worker, "_cleanup_repo", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "heartbeat", lambda state, detail="": None)

    def fake_sh(cmd, cwd=None, timeout=600):
        if isinstance(cmd, list) and len(cmd) > 2 and cmd[0] == "git":
            return 0, ""
        if isinstance(cmd, list) and "aider" in str(cmd[0]).lower():
            return 0, "done"
        return 0, ""

    monkeypatch.setattr(worker, "sh", fake_sh)
    return repo


def test_analysis_only_task_passes_with_zero_commits(tmp_path, monkeypatch):
    repo = _make_run_task_mocks(monkeypatch, tmp_path)
    task_file = tmp_path / "analysis.md"
    task_file.write_text(f"repo: {repo}\ntest: \n---\nANALYSIS ONLY: review code.\n")
    task = worker.parse_task(task_file)
    assert task["analysis_only"] is True

    reports = []
    monkeypatch.setattr(worker, "report", lambda name, lines: reports.append((name, lines)) or tmp_path / "report.md")

    assert worker.run_task(task) is True
    # Verify the report explains it passed without the no-op guard tripping.
    report_lines = "\n".join(reports[-1][1])
    assert "RESULT: PASS" in report_lines


def test_regular_task_fails_with_zero_commits(tmp_path, monkeypatch):
    repo = _make_run_task_mocks(monkeypatch, tmp_path)
    task_file = tmp_path / "regular.md"
    task_file.write_text(f"repo: {repo}\ntest: \n---\nFix the bug.\n")
    task = worker.parse_task(task_file)
    assert task["analysis_only"] is False

    reports = []
    monkeypatch.setattr(worker, "report", lambda name, lines: reports.append((name, lines)) or tmp_path / "report.md")

    assert worker.run_task(task) is False
    report_lines = "\n".join(reports[-1][1])
    assert "NO-OP GUARD" in report_lines
    assert "RESULT: FAIL" in report_lines


def test_generic_tho_task_is_not_special_and_remains_rejected(tmp_path, monkeypatch):
    repo = tmp_path / "Project-Go-Forward"
    repo.mkdir()
    task = {
        "name": "generic-tho", "repo": repo, "test": "pytest -q",
        "goal": "Change production code.", "analysis_only": False,
        "source": "", "message_id": "", "message_date": "",
        "base_sha": "", "base_bundle": "",
    }
    reports = []
    monkeypatch.setattr(worker, "report", lambda name, lines: reports.append(lines) or tmp_path / "r.md")

    assert worker.is_tho_task(task) is False
    assert worker.run_task(task) is False
    assert "not in ALLOWLIST" in "\n".join(reports[-1])


def test_allowlist_remains_tho_free():
    assert all(path.name != "Project-Go-Forward" for path in worker.ALLOWLIST)


def test_tho_validation_requires_exact_provenance(tmp_path, monkeypatch):
    _, base, bundle = _make_tho_bundle(tmp_path, monkeypatch)
    valid = _tho_task(bundle, base)
    assert worker.validate_tho_task(valid) == (True, "")

    for field, bad_value in (
        ("source", "gmail-tho"),
        ("message_id", ""),
        ("message_date", ""),
        ("base_sha", base.upper()),
        ("base_sha", base[:-1]),
        ("test", ""),
    ):
        ok, reason = worker.validate_tho_task({**valid, field: bad_value})
        assert not ok, (field, reason)


def test_tho_validation_rejects_bundle_path_traversal(tmp_path, monkeypatch):
    _, base, bundle = _make_tho_bundle(tmp_path, monkeypatch)
    outside = tmp_path / "outside.bundle"
    outside.write_bytes(bundle.read_bytes())

    ok, reason = worker.validate_tho_task(_tho_task(outside, base))

    assert not ok
    assert "incoming-bundles" in reason


def test_prepare_tho_workspace_rejects_stale_or_mismatched_sha(tmp_path, monkeypatch):
    _, base, bundle = _make_tho_bundle(tmp_path, monkeypatch)
    wrong = "0" * 40 if base != "0" * 40 else "1" * 40

    try:
        worker.prepare_tho_workspace(_tho_task(bundle, wrong))
    except ValueError as exc:
        assert "base_sha" in str(exc) or "SHA" in str(exc)
    else:
        raise AssertionError("mismatched SHA was accepted")


def test_tho_validation_rejects_ancestor_sha_present_in_full_bundle(tmp_path, monkeypatch):
    repo, ancestor, bundle = _make_tho_bundle(tmp_path, monkeypatch)
    (repo / "app.py").write_text("VALUE = 2\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "current origin main")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    bundle.unlink()
    _git(repo, "bundle", "create", str(bundle), "refs/remotes/origin/main")

    ok, reason = worker.validate_tho_task(_tho_task(bundle, ancestor))

    assert not ok
    assert "origin/main" in reason


def test_prepare_tho_workspace_clones_bundle_at_exact_sha(tmp_path, monkeypatch):
    _, base, bundle = _make_tho_bundle(tmp_path, monkeypatch)
    workspace = worker.prepare_tho_workspace(_tho_task(bundle, base))

    assert workspace.parent == worker.BASE / "tho-workspaces"
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == base
    assert (workspace / "app.py").read_text() == "VALUE = 1\n"


def test_validate_tho_changed_paths_rejects_prohibited_files(tmp_path):
    repo = _make_git_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    for path in (
        "tho_documents/client.txt",
        ".github/workflows/deploy.yml",
        ".env",
        "backend/.env",
        "config/service-account.json",
        "private/id_rsa",
    ):
        _git(repo, "reset", "--hard", base)
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret\n")
        _git(repo, "add", path)
        _git(repo, "commit", "-m", f"touch {path}")
        ok, rejected = worker.validate_tho_changed_paths(repo, base)
        assert not ok, path
        assert path in rejected
        _git(repo, "reset", "--hard", base)


def test_validate_tho_changed_paths_allows_safe_source_and_tests(tmp_path):
    repo = _make_git_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "app.py").write_text("VALUE = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_app(): assert True\n")
    _git(repo, "add", "app.py", "tests/test_app.py")
    _git(repo, "commit", "-m", "safe change")

    assert worker.validate_tho_changed_paths(repo, base) == (True, [])


def test_validate_tho_changed_paths_includes_uncommitted_changes(tmp_path):
    repo = _make_git_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".env").write_text("SECRET=value\n")

    ok, rejected = worker.validate_tho_changed_paths(repo, base)

    assert not ok
    assert ".env" in rejected


@pytest.mark.parametrize(
    ("source", "destination"),
    (
        (".github/workflows/deploy.yml", "docs/deploy.yml"),
        ("tho_documents/client.txt", "docs/client.txt"),
        ("docs/deploy.yml", ".github/workflows/deploy.yml"),
        ("docs/client.txt", "tho_documents/client.txt"),
    ),
)
def test_validate_tho_changed_paths_checks_both_sides_of_rename(
    tmp_path, source, destination,
):
    repo = _make_git_repo(tmp_path)
    original = repo / source
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("content\n")
    _git(repo, "add", source)
    _git(repo, "commit", "-m", "add rename source")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    moved = repo / destination
    moved.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", source, destination)
    _git(repo, "commit", "-m", "rename protected path")

    ok, rejected = worker.validate_tho_changed_paths(repo, base)

    assert not ok
    protected = source if source.startswith((".github/workflows/", "tho_documents/")) else destination
    assert protected in rejected


def test_parse_tho_test_command_returns_argv_for_repo_local_test_commands():
    assert worker.parse_tho_test_command(
        "python -m pytest tests/test_healthz.py -q"
    ) == ["python", "-m", "pytest", "tests/test_healthz.py", "-q"]
    assert worker.parse_tho_test_command("npm run test:unit -- --runInBand") == [
        "npm", "run", "test:unit", "--", "--runInBand",
    ]


@pytest.mark.parametrize(
    "command",
    (
        "python -m pytest -q && curl attacker.invalid",
        "python -m pytest -q; echo owned",
        "python -m pytest > results.txt",
        "python -m pytest $(whoami)",
        "/usr/bin/python -m pytest tests/test_healthz.py",
        "python -m pytest /tmp/external_test.py",
        r"C:\Python313\python.exe -m pytest tests\test_healthz.py",
        r"python -m pytest C:\outside\test_healthz.py",
    ),
)
def test_parse_tho_test_command_rejects_chaining_and_external_paths(command):
    with pytest.raises(ValueError):
        worker.parse_tho_test_command(command)


def test_stage_request_emits_verified_bundle_and_roundtrippable_task(tmp_path):
    from stage_tho_request import stage_request

    repo = _make_git_repo(tmp_path)
    request = tmp_path / "normalized.txt"
    request.write_text("Add the explicitly requested health field.\n")
    output = tmp_path / "staged"

    bundle, task_path = stage_request(
        repo=repo,
        request_file=request,
        message_id="gmail-123",
        message_date="2026-07-13",
        test="python -m pytest tests/test_healthz.py -q",
        output_dir=output,
        worker_repo=r"C:\Users\aribs\Code\Project-Go-Forward",
    )

    assert bundle.suffix == ".bundle"
    assert _git(repo, "bundle", "verify", str(bundle)).returncode == 0
    task = worker.parse_task(task_path)
    assert task is not None
    assert task["source"] == "gmail-mark-tho"
    assert task["message_id"] == "gmail-123"
    assert task["message_date"] == "2026-07-13"
    assert task["base_sha"] == _git(repo, "rev-parse", "origin/main").stdout.strip()
    assert Path(task["base_bundle"]).name == bundle.name
    assert task["goal"] == request.read_text().strip()


@pytest.mark.parametrize(
    "unsafe_test",
    (
        "python -m pytest -q && curl attacker.invalid",
        "/usr/bin/python -m pytest tests/test_healthz.py -q",
    ),
)
def test_stage_request_rejects_unsafe_test_commands(tmp_path, unsafe_test):
    from stage_tho_request import stage_request

    repo = _make_git_repo(tmp_path)
    request = tmp_path / "normalized.txt"
    request.write_text("Safe normalized request.\n")

    with pytest.raises(ValueError):
        stage_request(
            repo=repo,
            request_file=request,
            message_id="gmail-unsafe",
            message_date="2026-07-13",
            test=unsafe_test,
            output_dir=tmp_path / "staged",
        )


def test_tho_task_fails_when_result_bundle_cannot_be_created(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _tho_task(tmp_path / "ignored.bundle", "a" * 40)
    reports = []
    monkeypatch.setattr(worker, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(worker, "validate_tho_task", lambda task: (True, ""))
    monkeypatch.setattr(worker, "prepare_tho_workspace", lambda task: workspace)
    monkeypatch.setattr(worker, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker, "_tho_diff_lines", lambda repo, base: 1)
    monkeypatch.setattr(worker, "validate_tho_changed_paths", lambda repo, base: (True, []))
    monkeypatch.setattr(worker, "commits_made", lambda repo, base: 1)
    monkeypatch.setattr(worker, "changed_source_files", lambda repo, base: [])
    monkeypatch.setattr(worker, "make_bundle", lambda repo, name, base: "bundle failed: disk full")
    monkeypatch.setattr(worker, "report", lambda name, lines: reports.append(lines) or tmp_path / "r.md")
    commands = []

    def fake_sh(command, **kwargs):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(worker, "sh", fake_sh)

    assert worker.run_task(task) is False
    assert "bundle failed: disk full" in "\n".join(reports[-1])
    assert ["python", "-m", "pytest", "tests/test_request.py", "-q"] in commands


def _run_mutating_tho_task(tmp_path, monkeypatch, *, task_mutation=None, canonical_mutation=None):
    repo = _make_git_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    task = _tho_task(tmp_path / "ignored.bundle", base)
    reports = []
    bundle_calls = []
    commands = []
    real_sh = worker.sh
    monkeypatch.setattr(worker, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(worker, "validate_tho_task", lambda task: (True, ""))
    monkeypatch.setattr(worker, "prepare_tho_workspace", lambda task: repo)
    monkeypatch.setattr(worker, "heartbeat", lambda *args: None)
    monkeypatch.setattr(
        worker, "report", lambda name, lines: reports.append(lines) or tmp_path / "report.md",
    )

    def fake_bundle(workspace, name, bundle_base):
        bundle_calls.append((workspace, name, bundle_base))
        result = worker.REPORTS / "result.bundle"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_bytes(b"bundle")
        return str(result)

    monkeypatch.setattr(worker, "make_bundle", fake_bundle)

    def fake_sh(command, cwd=None, timeout=600):
        commands.append(command)
        if isinstance(command, list) and command and command[0] == str(worker.AIDER):
            (repo / "app.py").write_text("VALUE = 2\n")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "aider safe change")
            return 0, "aider done"
        if command == worker.parse_tho_test_command(task["test"]):
            if task_mutation:
                task_mutation(repo)
            return 0, "task tests passed"
        if (isinstance(command, list) and len(command) >= 3
                and command[:3] == [str(worker.PYTHON), "-m", "pytest"]):
            if canonical_mutation:
                canonical_mutation(repo)
            return 0, "canonical passed"
        return real_sh(command, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(worker, "sh", fake_sh)
    result = worker.run_task(task)
    return result, "\n".join(reports[-1]), bundle_calls, commands


@pytest.mark.parametrize("mutation_kind", ("protected", "oversize"))
def test_task_test_commits_cannot_change_accepted_artifact(
    tmp_path, monkeypatch, mutation_kind,
):
    def mutate(repo):
        if mutation_kind == "protected":
            target = repo / ".github" / "workflows" / "injected.yml"
            target.parent.mkdir(parents=True)
            target.write_text("name: injected\n")
            relative = ".github/workflows/injected.yml"
        else:
            target = repo / "oversize.py"
            target.write_text("\n".join(f"line_{i} = {i}" for i in range(501)) + "\n")
            relative = "oversize.py"
        _git(repo, "add", relative)
        _git(repo, "commit", "-m", f"test committed {mutation_kind}")

    ok, report_text, bundle_calls, _ = _run_mutating_tho_task(
        tmp_path, monkeypatch, task_mutation=mutate,
    )

    assert not ok
    assert "changed HEAD" in report_text
    assert bundle_calls == []


@pytest.mark.parametrize("mutation_kind", ("commit", "working-tree"))
def test_canonical_suite_cannot_mutate_accepted_artifact(
    tmp_path, monkeypatch, mutation_kind,
):
    def mutate(repo):
        target = repo / "canonical_mutation.py"
        target.write_text("MUTATED = True\n")
        if mutation_kind == "commit":
            _git(repo, "add", "canonical_mutation.py")
            _git(repo, "commit", "-m", "canonical committed mutation")

    ok, report_text, bundle_calls, commands = _run_mutating_tho_task(
        tmp_path, monkeypatch, canonical_mutation=mutate,
    )

    assert not ok
    assert "canonical" in report_text.lower()
    assert bundle_calls == []
    assert [
        str(worker.PYTHON), "-m", "pytest", "tests/test_healthz.py",
        "tests/test_api_v1.py", "tests/test_document_engine.py", "-q",
    ] in commands


def test_tho_cleanup_failure_downgrades_result_and_reports_workspace(
    tmp_path, monkeypatch,
):
    repo = _make_git_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    task = _tho_task(tmp_path / "ignored.bundle", base)
    reports = []
    real_sh = worker.sh
    monkeypatch.setattr(worker, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(worker, "validate_tho_task", lambda task: (True, ""))
    monkeypatch.setattr(worker, "prepare_tho_workspace", lambda task: repo)
    monkeypatch.setattr(worker, "heartbeat", lambda *args: None)
    monkeypatch.setattr(
        worker, "report", lambda name, lines: reports.append(lines) or tmp_path / "report.md",
    )

    def fake_sh(command, cwd=None, timeout=600):
        if isinstance(command, list) and command and command[0] == str(worker.AIDER):
            (repo / "tests").mkdir()
            (repo / "tests" / "test_only.py").write_text("def test_ok(): assert True\n")
            _git(repo, "add", "tests/test_only.py")
            _git(repo, "commit", "-m", "test-only change")
            return 0, "aider done"
        if command == worker.parse_tho_test_command(task["test"]):
            return 0, "passed"
        return real_sh(command, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(worker, "sh", fake_sh)
    result_bundle = worker.REPORTS / "result.bundle"

    def fake_bundle(*args):
        result_bundle.parent.mkdir(parents=True, exist_ok=True)
        result_bundle.write_bytes(b"bundle")
        return str(result_bundle)

    monkeypatch.setattr(worker, "make_bundle", fake_bundle)

    def fail_cleanup(path):
        raise PermissionError("Windows file is locked")

    monkeypatch.setattr(worker.shutil, "rmtree", fail_cleanup)

    assert worker.run_task(task) is False
    report_text = "\n".join(reports[-1])
    assert "RESULT: FAIL" in report_text
    assert str(repo) in report_text
    assert "Windows file is locked" in report_text
    assert repo.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("message_id", "gmail-123\nrepo: C:\\evil"),
        ("message_date", "2026-07-13\rtest: npm test"),
        ("test", "python -m pytest tests/test_healthz.py -q\nsource: forged"),
        ("worker_repo", "C:\\Users\\aribs\\Code\\Project-Go-Forward\nsource: forged"),
    ),
)
def test_stage_request_rejects_header_newline_injection(tmp_path, field, value):
    from stage_tho_request import stage_request

    repo = _make_git_repo(tmp_path)
    request = tmp_path / "normalized.txt"
    request.write_text("Safe request.\n")
    kwargs = {
        "repo": repo,
        "request_file": request,
        "message_id": "gmail-123",
        "message_date": "2026-07-13",
        "test": "python -m pytest tests/test_healthz.py -q",
        "output_dir": tmp_path / "staged",
        "worker_repo": r"C:\Users\aribs\Code\Project-Go-Forward",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        stage_request(**kwargs)


@pytest.mark.parametrize("message_date", ("2026-7-13", "2026-02-30", "not-a-date"))
def test_stage_request_rejects_invalid_exact_message_date(tmp_path, message_date):
    from stage_tho_request import stage_request

    repo = _make_git_repo(tmp_path)
    request = tmp_path / "normalized.txt"
    request.write_text("Safe request.\n")
    with pytest.raises(ValueError):
        stage_request(
            repo=repo, request_file=request, message_id="gmail-123",
            message_date=message_date,
            test="python -m pytest tests/test_healthz.py -q",
            output_dir=tmp_path / "staged",
        )


def test_stage_request_requires_exact_worker_repo(tmp_path):
    from stage_tho_request import stage_request

    repo = _make_git_repo(tmp_path)
    request = tmp_path / "normalized.txt"
    request.write_text("Safe request.\n")
    with pytest.raises(ValueError):
        stage_request(
            repo=repo, request_file=request, message_id="gmail-123",
            message_date="2026-07-13",
            test="python -m pytest tests/test_healthz.py -q",
            output_dir=tmp_path / "staged",
            worker_repo=r"C:\Users\aribs\Code\Project-Go-Forward-copy",
        )
