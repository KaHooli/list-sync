#!/usr/bin/env python3
"""
Run every suite in this directory and summarise the result.

Each suite is a standalone script that prints one line per check and exits
non-zero if any of them failed, so they can be run individually while
debugging. This runner exists so CI has a single command.

Suites run in separate processes on purpose: several of them stub out heavy
dependencies or monkeypatch modules, and a shared interpreter would let one
suite's stubs leak into the next.

Usage:
    python tests/run_all.py           # run everything
    python tests/run_all.py db api    # run suites whose name contains db or api
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def discover(patterns):
    """Find the suites to run, optionally filtered by substring."""
    names = sorted(
        f[:-3] for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py")
    )
    if patterns:
        names = [n for n in names if any(p in n for p in patterns)]
    return names


def run(name):
    """Run one suite, returning (ok, checks_passed, output)."""
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, name + ".py")],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    elapsed = time.monotonic() - started
    output = proc.stdout + proc.stderr
    passed = sum(1 for line in output.splitlines() if line.startswith("PASS"))
    return proc.returncode == 0, passed, elapsed, output


def main():
    suites = discover(sys.argv[1:])
    if not suites:
        print("No suites matched")
        return 1

    total = 0
    failed = []

    for name in suites:
        ok, passed, elapsed, output = run(name)
        total += passed
        if ok:
            print(f"ok      {name:<24} {passed:>3} checks  {elapsed:5.1f}s")
        else:
            failed.append(name)
            print(f"FAILED  {name:<24} {passed:>3} passed  {elapsed:5.1f}s")
            # Only the failures are worth the reader's attention.
            for line in output.splitlines():
                if line.startswith("FAIL") or "Error" in line or "Traceback" in line:
                    print(f"          {line}")

    print()
    print(f"{total} checks passed across {len(suites)} suites")
    if failed:
        print(f"failing suites: {', '.join(failed)}")
        print()
        print("Re-run one on its own to see its full output:")
        print(f"    python tests/{failed[0]}.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
