"""Exercise the per-list user persistence against a real sqlite db."""
import os, sys, tempfile, types

tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# helpers.py imports seleniumbase at module scope; none of the code under test
# touches a browser, so stub it out rather than installing Chrome.
for name in ("seleniumbase", "bs4", "dotenv"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            mod = types.ModuleType(name)
            for attr in ("SB", "BeautifulSoup", "load_dotenv", "set_key", "find_dotenv"):
                setattr(mod, attr, lambda *a, **k: None)
            sys.modules[name] = mod

import list_sync.database as db
db.DB_FILE = os.path.join(tmp, "list_sync.db")
db.init_database()

from list_sync.database import (
    save_list_id, load_list_ids, get_list_user_id, update_list_user_id,
    normalize_list_id, update_list_sync_info,
)

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        fail.append(label)

# --- normalisation ---
check("imdb bare id", normalize_list_id("imdb", "ls123456789"), "ls123456789")
check("imdb full url", normalize_list_id("imdb", "https://www.imdb.com/list/ls123456789/"), "ls123456789")
check("imdb watchlist", normalize_list_id("imdb", "https://www.imdb.com/user/ur99887766/watchlist"), "ur99887766")
check("imdb chart", normalize_list_id("imdb", "https://www.imdb.com/chart/top/"), "top")
check("trakt url trailing slash",
      normalize_list_id("trakt", "https://trakt.tv/users/me/lists/faves/"),
      normalize_list_id("trakt", "http://www.trakt.tv/users/me/lists/faves"))

# --- assign a user, then re-save without one ---
save_list_id("ls123456789", "imdb", user_id="7")
check("user assigned on add", get_list_user_id("imdb", "ls123456789"), "7")

# lookup via the URL form must find the same list
check("lookup by url form", get_list_user_id("imdb", "https://www.imdb.com/list/ls123456789/"), "7")

# a re-save that says nothing about users must not reset the user
save_list_id("ls123456789", "imdb")
check("re-save preserves user", get_list_user_id("imdb", "ls123456789"), "7")

# ...and must not wipe last_synced
update_list_sync_info("imdb", "ls123456789", 42)
before = [l for l in load_list_ids() if l["id"] == "ls123456789"][0]
save_list_id("ls123456789", "imdb")
after = [l for l in load_list_ids() if l["id"] == "ls123456789"][0]
check("re-save preserves last_synced", after["last_synced"] is not None and after["last_synced"] == before["last_synced"], True)
check("no duplicate row created", len([l for l in load_list_ids() if l["id"] == "ls123456789"]), 1)

# --- reassignment ---
check("reassign by url form", update_list_user_id("imdb", "https://www.imdb.com/list/ls123456789", "3"), True)
check("reassignment took", get_list_user_id("imdb", "ls123456789"), "3")
check("reassign unknown list", update_list_user_id("imdb", "ls000000000", "3"), False)
check("unknown list has no user", get_list_user_id("imdb", "ls000000000"), None)

# --- explicit user on re-save still wins ---
save_list_id("ls123456789", "imdb", user_id="9")
check("explicit user overrides", get_list_user_id("imdb", "ls123456789"), "9")

# --- legacy duplicate rows (bare id + url) both get reassigned ---
import sqlite3
with sqlite3.connect(db.DB_FILE) as conn:
    conn.execute("INSERT INTO lists (list_type, list_id, list_url, item_count, user_id) VALUES (?,?,?,?,?)",
                 ("imdb", "https://www.imdb.com/list/ls123456789", "x", 0, "1"))
update_list_user_id("imdb", "ls123456789", "5")
users = sorted(l["user_id"] for l in load_list_ids() if normalize_list_id("imdb", l["id"]) == "ls123456789")
check("both duplicate rows reassigned", users, ["5", "5"])

# --- lists with no user column value default to admin ---
with sqlite3.connect(db.DB_FILE) as conn:
    conn.execute("INSERT INTO lists (list_type, list_id, user_id) VALUES ('trakt','12345',NULL)")
check("null user defaults to admin", get_list_user_id("trakt", "12345"), "1")

print()
print("FAILED:" , fail if fail else "none")
sys.exit(1 if fail else 0)
