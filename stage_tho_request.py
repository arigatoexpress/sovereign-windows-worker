"""Stage an explicit THO request and exact origin/main bundle on the Mac.

This tool has no Gmail or network integration. It consumes already-normalized text
and a clone whose origin/main reference has already been refreshed by the operator.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from worker import parse_tho_test_command


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    )
    return completed.stdout.strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "request"


def stage_request(
    *,
    repo: Path,
    request_file: Path,
    message_id: str,
    message_date: str,
    test: str,
    output_dir: Path,
    worker_repo: str = r"C:\Users\aribs\Code\Project-Go-Forward",
) -> tuple[Path, Path]:
    """Create a verified full bundle and its round-trippable worker task file."""
    repo = repo.resolve()
    request_file = request_file.resolve()
    if not repo.is_dir() or not request_file.is_file():
        raise ValueError("repo and normalized request file must exist")
    if not message_id.strip() or not message_date.strip() or not test.strip():
        raise ValueError("message_id, message_date, and test are required")
    parse_tho_test_command(test)
    goal = request_file.read_text(encoding="utf-8").strip()
    if not goal:
        raise ValueError("normalized request must not be empty")

    base_sha = _git(repo, "rev-parse", "origin/main")
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise ValueError("origin/main did not resolve to a full commit SHA")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"tho-{message_date}-{_slug(message_id)}"
    bundle = output_dir / f"{stem}.bundle"
    task = output_dir / f"{stem}.md"
    if bundle.exists() or task.exists():
        raise FileExistsError(f"staged artifact already exists for {stem}")

    _git(repo, "bundle", "create", str(bundle), "origin/main")
    try:
        _git(repo, "bundle", "verify", str(bundle))
    except Exception:
        bundle.unlink(missing_ok=True)
        raise

    headers = (
        f"repo: {worker_repo}\n"
        "source: gmail-mark-tho\n"
        f"message_id: {message_id.strip()}\n"
        f"message_date: {message_date.strip()}\n"
        f"base_sha: {base_sha}\n"
        f"base_bundle: {bundle.name}\n"
        f"test: {test.strip()}\n"
        "---\n"
    )
    task.write_text(headers + goal + "\n", encoding="utf-8")
    return bundle, task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--message-date", required=True)
    parser.add_argument("--test", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--worker-repo", default=r"C:\Users\aribs\Code\Project-Go-Forward",
    )
    args = parser.parse_args()
    bundle, task = stage_request(
        repo=args.repo,
        request_file=args.request_file,
        message_id=args.message_id,
        message_date=args.message_date,
        test=args.test,
        output_dir=args.output_dir,
        worker_repo=args.worker_repo,
    )
    print(bundle)
    print(task)


if __name__ == "__main__":
    main()
