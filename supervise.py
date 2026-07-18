"""supervise.py — restart-forever supervisor for the sovereign worker.

Replaces run_worker.bat, which had a fatal unattended-service flaw: a console
control event (CTRL_C / CTRL_BREAK / console close) made cmd.exe print
"Terminate batch job (Y/N)?" and block forever, because the task runs under S4U
logon with no interactive console to answer it. The restart loop was defeated by
exactly the event it existed to survive.

Observed 2026-07-18: starting the scheduled task over SSH ties the launched tree
to that session's console. When the SSH session closed, the control event
reached both children — worker.py died with KeyboardInterrupt and cmd.exe hung
on the Y/N prompt. Measured directly: worker alive at 16:55:42 with the session
open, dead seconds after it closed, KeyboardInterrupt count 8 -> 9, wrapper
process still alive but never looping again. The log showed 16 starts against 9
exits: seven runs were interrupted before they could even record an exit.

Two independent defences here, because either alone still loses work:

1. The supervisor ignores console control events, so it keeps looping.
2. The worker is launched in its OWN process group, so a control event
   delivered to the supervisor's console never reaches it at all.

Deliberately not solved with WSL or a Windows service wrapper: this is one
misplaced batch file, not a runtime problem, and both alternatives add a
dependency the charter would have to justify.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "worker.py")
LOG_PATH = os.path.join(HERE, "worker.log")
LOG_MAX_BYTES = 100 * 1024 * 1024
RESTART_DELAY_S = 30

ENV_DEFAULTS = {
    "SOV_WORKER_MODEL": "ollama/qwen3-coder:30b",
    "SOV_WORKER_WEAK_MODEL": "ollama/gemma3:4b",
    "SOV_WORKER_RELEASE": "canonical-worker-v6",
    "SOV_WORKER_THO_ENABLED": "0",
}


def ignore_console_signals():
    """Make the supervisor itself immune to console control events.

    Without this the supervisor dies alongside the worker and nothing restarts
    it. SIGBREAK is Windows-only; SIGINT covers both platforms.
    """
    installed = []
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, signal.SIG_IGN)
            installed.append(name)
        except (ValueError, OSError):
            # Not on the main thread, or unsupported — the process-group
            # isolation below is the load-bearing defence regardless.
            pass
    return installed


def child_creationflags():
    """Flags that detach the worker from the supervisor's console group.

    CREATE_NEW_PROCESS_GROUP means a CTRL_C aimed at our console is not
    delivered to the worker. Zero on non-Windows, where the concept and the
    failure mode do not exist.
    """
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def rotate_log(path=LOG_PATH, max_bytes=LOG_MAX_BYTES):
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
            return True
    except OSError:
        pass
    return False


def build_env(base=None):
    env = dict(os.environ if base is None else base)
    for k, v in ENV_DEFAULTS.items():
        env.setdefault(k, v)
    return env


def _stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_forever(run_once, sleep=time.sleep, log=None, max_iterations=None):
    """The restart loop, isolated from process machinery so it is testable.

    ``run_once`` returns the worker's exit code. ``max_iterations`` bounds the
    loop for tests; production passes None and loops forever.
    """
    log = log or (lambda _m: None)
    n = 0
    while max_iterations is None or n < max_iterations:
        n += 1
        log(f"[{_stamp()}] worker starting (supervised)")
        try:
            rc = run_once()
        except Exception as e:  # a crashing launch must not kill the loop
            log(f"[{_stamp()}] worker launch failed: {type(e).__name__}: {e}")
            rc = None
        log(f"[{_stamp()}] worker exited rc={rc} - restarting in "
            f"{RESTART_DELAY_S}s")
        sleep(RESTART_DELAY_S)
    return n


def main():
    ignore_console_signals()
    env = build_env()
    flags = child_creationflags()

    def run_once():
        rotate_log()
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                [sys.executable, WORKER],
                cwd=HERE,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            return proc.wait()

    def log(msg):
        try:
            with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(msg + "\n")
        except OSError:
            pass

    run_forever(run_once, log=log)


if __name__ == "__main__":
    main()
