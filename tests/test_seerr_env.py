"""SEERR_* must be preferred, OVERSEERR_* must keep working."""
import os, sys, types

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

from list_sync.config import get_seerr_env, LEGACY_ENV_NAMES
import list_sync.config as cfg

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

ALL = list(LEGACY_ENV_NAMES) + list(LEGACY_ENV_NAMES.values())
def clear():
    for v in ALL:
        os.environ.pop(v, None)
    cfg._reported_legacy_names.clear()

# --- the new name works ---
clear()
os.environ["SEERR_URL"] = "http://seerr:5055"
check("new name read", get_seerr_env("SEERR_URL"), "http://seerr:5055")

# --- the old name still works: an existing .env keeps the install running ---
clear()
os.environ["OVERSEERR_URL"] = "http://seerr:5055"
check("old name still honoured", get_seerr_env("SEERR_URL"), "http://seerr:5055")

clear()
os.environ["OVERSEERR_API_KEY"] = "abc123"
check("old api key honoured", get_seerr_env("SEERR_API_KEY"), "abc123")
clear()
os.environ["OVERSEERR_USER_ID"] = "7"
check("old user id honoured", get_seerr_env("SEERR_USER_ID", "1"), "7")
clear()
os.environ["OVERSEERR_4K"] = "true"
check("old 4k honoured", get_seerr_env("SEERR_4K", "false"), "true")

# --- the new name wins when both are set ---
clear()
os.environ["SEERR_URL"] = "http://new:5055"
os.environ["OVERSEERR_URL"] = "http://old:5055"
check("new name wins over old", get_seerr_env("SEERR_URL"), "http://new:5055")

# --- THE REGRESSION THAT MATTERS ---
# docker-compose writes ${SEERR_URL:-} as an empty string when unset. If that
# counted as a value it would shadow a populated OVERSEERR_URL and break every
# existing deployment on upgrade - the exact thing the fallback prevents.
clear()
os.environ["SEERR_URL"] = ""
os.environ["OVERSEERR_URL"] = "http://seerr:5055"
check("empty new name does not shadow old", get_seerr_env("SEERR_URL"), "http://seerr:5055")

clear()
os.environ["SEERR_API_KEY"] = "   "
os.environ["OVERSEERR_API_KEY"] = "realkey"
check("whitespace new name does not shadow old", get_seerr_env("SEERR_API_KEY"), "realkey")

# ...and an empty old name falls through to the default rather than returning ""
clear()
os.environ["OVERSEERR_USER_ID"] = ""
check("empty old name falls to default", get_seerr_env("SEERR_USER_ID", "1"), "1")

# The same trap with a default baked into compose instead of an empty string.
# ${SEERR_USER_ID:-1} injects a real "1", which legitimately outranks the old
# name - and silently reverts a configured requester to the admin account,
# which is the exact bug per-list users exist to prevent. The compose files
# therefore pass ${SEERR_USER_ID:-} and let the application default instead.
clear()
os.environ["OVERSEERR_USER_ID"] = "3"
check("legacy user id survives an unset new name",
      get_seerr_env("SEERR_USER_ID", "1"), "3")
clear()
os.environ["SEERR_USER_ID"] = ""          # what ${SEERR_USER_ID:-} produces
os.environ["OVERSEERR_USER_ID"] = "3"
check("legacy user id survives compose's empty new name",
      get_seerr_env("SEERR_USER_ID", "1"), "3")
clear()
os.environ["OVERSEERR_4K"] = "true"
check("legacy 4k survives an unset new name",
      get_seerr_env("SEERR_4K", "false"), "true")

# --- neither set ---
clear()
check("neither set returns default", get_seerr_env("SEERR_URL", "fallback"), "fallback")
check("neither set, no default", get_seerr_env("SEERR_URL"), None)

# --- a name with no legacy partner is just read straight ---
clear()
os.environ["SYNC_INTERVAL"] = "12"
check("unmapped name read directly", get_seerr_env("SYNC_INTERVAL", "24"), "12")
os.environ.pop("SYNC_INTERVAL", None)   # not in LEGACY_ENV_NAMES, so clear() misses it
check("unmapped name default", get_seerr_env("SYNC_INTERVAL", "24"), "24")

# --- the deprecation notice is said once, not on every read ---
clear()
os.environ["OVERSEERR_URL"] = "http://seerr:5055"
logging.disable(logging.NOTSET)
seen = []
class Catch(logging.Handler):
    def emit(self, record): seen.append(record.getMessage())
root = logging.getLogger()
root.addHandler(Catch()); root.setLevel(logging.WARNING)
for _ in range(5):
    get_seerr_env("SEERR_URL")
check("warned exactly once over five reads",
      sum(1 for m in seen if "OVERSEERR_URL" in m), 1)
check("warning names both spellings",
      all(k in seen[0] for k in ("OVERSEERR_URL", "SEERR_URL")), True)
logging.disable(logging.CRITICAL)

# --- every mapped pair is exercised above ---
check("all four settings mapped", sorted(LEGACY_ENV_NAMES),
      ["SEERR_4K", "SEERR_API_KEY", "SEERR_URL", "SEERR_USER_ID"])

clear()
print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
