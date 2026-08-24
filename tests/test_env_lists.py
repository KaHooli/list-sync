"""End-to-end: env vars -> database -> per-list requester."""
import sys, types, os, tempfile

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def stub(name, attrs=()):
    m = types.ModuleType(name)
    for a in attrs: setattr(m, a, type(a, (), {}))
    sys.modules[name] = m
    return m
for n in ("seleniumbase", "bs4", "halo"):
    try: __import__(n)
    except ImportError: stub(n, ("SB", "BeautifulSoup", "Halo"))
c = stub("cryptography"); f = stub("cryptography.fernet", ("Fernet", "InvalidToken")); c.fernet = f
d = stub("dotenv"); d.load_dotenv = lambda *a, **k: None; d.set_key = lambda *a, **k: None

tmp = tempfile.mkdtemp()
import list_sync.utils.logger as lg
lg.DATA_DIR = tmp
import list_sync.database as db
db.DB_FILE = os.path.join(tmp, "list_sync.db")
db.init_database()

import list_sync.config as cfg
# Force the environment fallback rather than ConfigManager (which needs a key).
class BrokenConfigManager(Exception): pass
cfg.ConfigManager = lambda *a, **k: (_ for _ in ()).throw(BrokenConfigManager())

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

os.environ["OVERSEERR_USER_ID"] = "4"
os.environ["IMDB_LISTS"] = "ls111111111::7, ls222222222 , https://www.imdb.com/list/ls333333333/::9"
os.environ["TRAKT_SPECIAL_LISTS"] = "trending:movies::5"

cfg.load_env_lists()

check("explicit user honoured", db.get_list_user_id("imdb", "ls111111111"), "7")
check("bare entry uses OVERSEERR_USER_ID", db.get_list_user_id("imdb", "ls222222222"), "4")
check("url entry with user", db.get_list_user_id("imdb", "ls333333333"), "9")
check("trakt special keeps its colon", db.get_list_user_id("trakt_special", "trending:movies"), "5")

stored = {l["id"] for l in db.load_list_ids()}
check("trakt special stored intact", "trending:movies" in stored, True)
check("list count", len(stored), 4)

# Re-running must not duplicate lists or reset users.
cfg.load_env_lists()
check("no duplicates on rerun", len(db.load_list_ids()), 4)
check("users survive rerun", db.get_list_user_id("imdb", "ls111111111"), "7")

# Changing the env user reassigns the existing list.
os.environ["IMDB_LISTS"] = "ls111111111::3, ls222222222"
cfg.load_env_lists()
check("env change reassigns", db.get_list_user_id("imdb", "ls111111111"), "3")
check("still no duplicates", len(db.load_list_ids()), 4)

# A list assigned in the UI must not be clobbered by a bare env entry.
db.update_list_user_id("imdb", "ls222222222", "8")
cfg.load_env_lists()
check("bare env entry leaves UI choice alone", db.get_list_user_id("imdb", "ls222222222"), "8")

# The URL form in env matches the stored list rather than adding a second row.
os.environ["IMDB_LISTS"] = "https://www.imdb.com/list/ls111111111"
cfg.load_env_lists()
check("url form does not duplicate", len(db.load_list_ids()), 4)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
