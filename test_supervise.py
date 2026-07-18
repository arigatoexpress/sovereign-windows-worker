"""Evals for the supervisor that replaced run_worker.bat.

The bug being fenced: a console control event reached the batch wrapper, which
blocked forever on "Terminate batch job (Y/N)?" with no console to answer it,
so the restart-forever loop never restarted anything. Measured 2026-07-18 —
16 worker starts against 9 recorded exits, and a wrapper process alive but
permanently stuck.
"""
from __future__ import annotations

import signal
import subprocess

import supervise


def test_worker_is_launched_in_its_own_process_group():
    """The load-bearing defence: a CTRL_C to our console must not reach it.

    This is what actually killed the worker every time an agent restarted it
    over SSH — the launched tree shared the session console, so the worker took
    the control event when that session closed.
    """
    expected = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    assert supervise.child_creationflags() == expected
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        assert supervise.child_creationflags() != 0, (
            "on Windows the worker must be detached from the console group")


def test_supervisor_ignores_console_signals():
    """If the supervisor dies with the worker, nothing restarts it."""
    original = signal.getsignal(signal.SIGINT)
    try:
        supervise.ignore_console_signals()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, original)


def test_restart_loop_survives_a_crashing_worker():
    """rc != 0 must restart, not exit — the whole point of the supervisor."""
    calls, slept = [], []
    supervise.run_forever(
        run_once=lambda: calls.append(1) or 1,
        sleep=slept.append, max_iterations=3)
    assert len(calls) == 3
    assert slept == [supervise.RESTART_DELAY_S] * 3


def test_restart_loop_survives_a_launch_exception():
    """A failure to even spawn must not kill the supervisor.

    The batch wrapper's equivalent path was the Y/N hang: one bad event and the
    loop was over for good.
    """
    calls = []

    def boom():
        calls.append(1)
        raise OSError("spawn failed")

    n = supervise.run_forever(boom, sleep=lambda _s: None, max_iterations=2)
    assert n == 2 and len(calls) == 2


def test_exit_codes_are_recorded():
    """Silent restarts are how a crash-loop hides; every exit gets a line."""
    lines = []
    supervise.run_forever(run_once=lambda: 3, sleep=lambda _s: None,
                          log=lines.append, max_iterations=1)
    assert any("worker starting" in ln for ln in lines)
    assert any("rc=3" in ln for ln in lines)


def test_env_defaults_pin_the_verified_model_and_release():
    env = supervise.build_env(base={})
    assert env["SOV_WORKER_MODEL"] == "ollama/qwen3-coder:30b"
    assert env["SOV_WORKER_RELEASE"] == "canonical-worker-v6"
    assert env["SOV_WORKER_THO_ENABLED"] == "0", "THO stays fenced off"


def test_env_does_not_override_an_explicit_operator_value():
    env = supervise.build_env(base={"SOV_WORKER_MODEL": "ollama/devstral:24b"})
    assert env["SOV_WORKER_MODEL"] == "ollama/devstral:24b"


def test_log_rotation_only_fires_above_the_cap(tmp_path):
    log = tmp_path / "worker.log"
    log.write_text("x" * 50)
    assert supervise.rotate_log(str(log), max_bytes=1000) is False
    assert supervise.rotate_log(str(log), max_bytes=10) is True
    assert (tmp_path / "worker.log.1").exists()
