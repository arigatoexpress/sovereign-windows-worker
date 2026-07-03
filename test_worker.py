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
