"""Unit tests for the sovereign windows worker (pure functions only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import worker


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
