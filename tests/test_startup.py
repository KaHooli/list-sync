"""Startup must survive Seerr being slow, and never prompt in a container."""
import sys, types, os

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def stub(n, a=()):
    m = types.ModuleType(n)
    for x in a: setattr(m, x, type(x, (), {}))
    sys.modules[n] = m; return m
for n in ("seleniumbase", "bs4", "halo"):
    try: __import__(n)
    except ImportError: stub(n, ("SB", "BeautifulSoup", "Halo"))
c = stub("cryptography"); f = stub("cryptography.fernet", ("Fernet", "InvalidToken")); c.fernet = f
d = stub("dotenv"); d.load_dotenv = lambda *a, **k: None; d.set_key = lambda *a, **k: None

import logging
logging.disable(logging.CRITICAL)

import list_sync.main as m

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

# m.time is the real time module, so patches here are global - keep the
# originals so they can be put back exactly.
REAL_SLEEP = m.time.sleep
REAL_MONOTONIC = m.time.monotonic

slept = []
m.time.sleep = lambda s: slept.append(s)

GOOD = ("http://seerr:5055", "KEY", "1", 24.0, True, False)
DEAD = (None, None, None, 0.0, False, False)

# --- reachable immediately ---
m.load_env_config = lambda: GOOD
slept.clear()
check("immediate success", m._load_env_config_with_retry()[:2], ("http://seerr:5055", "KEY"))
check("no sleeping when healthy", slept, [])

# --- unreachable for the first 3 attempts, then comes up ---
state = {"n": 0}
def flaky():
    state["n"] += 1
    return GOOD if state["n"] > 3 else DEAD
m.load_env_config = flaky
slept.clear()
got = m._load_env_config_with_retry()
check("recovers after transient outage", got[:2], ("http://seerr:5055", "KEY"))
check("attempts made", state["n"], 4)
check("backoff grew", slept, [2, 4, 8])

# --- never comes up: must give up rather than spin forever ---
m.load_env_config = lambda: DEAD
slept.clear()
check("gives up eventually", m._load_env_config_with_retry(total_seconds=0), (None, None, None))

# backoff is capped so a long outage doesn't sleep for hours
clock = {"t": 0.0}
def fake_monotonic():
    clock["t"] += 5.0
    return clock["t"]
m.time.monotonic = fake_monotonic
m.load_env_config = lambda: DEAD
slept.clear()
m._load_env_config_with_retry(total_seconds=200)
check("backoff capped at 30s", max(slept), 30)
check("stopped once past the deadline", clock["t"] >= 200, True)
m.time.monotonic = REAL_MONOTONIC

# --- interactivity detection ---
for var in ("AUTOMATED_MODE", "RUNNING_IN_DOCKER"):
    os.environ.pop(var, None)

class FakeStdin:
    def __init__(self, tty): self._tty = tty
    def isatty(self): return self._tty

real_stdin = m.sys.stdin

m.sys.stdin = FakeStdin(True)
check("tty -> interactive", m._is_interactive(), True)

m.sys.stdin = FakeStdin(False)
check("no tty -> not interactive", m._is_interactive(), False)

m.sys.stdin = FakeStdin(True)
os.environ["AUTOMATED_MODE"] = "true"
check("automated mode -> not interactive", m._is_interactive(), False)
os.environ["AUTOMATED_MODE"] = "false"
os.environ["RUNNING_IN_DOCKER"] = "true"
check("docker -> not interactive", m._is_interactive(), False)
os.environ.pop("RUNNING_IN_DOCKER")

m.sys.stdin = None
check("no stdin at all -> not interactive", m._is_interactive(), False)
m.sys.stdin = real_stdin

# --- the actual bug: container with unreachable Seerr must exit, not EOF ---
# Stub the retry helper rather than let it spin for its full window: this case
# is about what get_credentials does once retrying has already given up.
os.environ["RUNNING_IN_DOCKER"] = "true"
m._load_env_config_with_retry = lambda *a, **k: (None, None, None)
m.load_config = lambda: (None, None, None)
def boom(*a, **k):
    raise EOFError("EOF when reading a line")
m.custom_input = boom

try:
    m.get_credentials(  )
    check("container exits cleanly", "returned", "SystemExit")
except SystemExit as e:
    check("container exits cleanly", e.code, 1)
except EOFError:
    check("container exits cleanly", "EOFError (the bug)", "SystemExit")

m.time.sleep = REAL_SLEEP

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
