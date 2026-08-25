"""Drive the list-user endpoints through FastAPI's test client."""
import sys, types, os, tempfile

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def stub(name, attrs=()):
    m = types.ModuleType(name)
    for a in attrs:
        setattr(m, a, type(a, (), {}))
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
db.save_seerr_users([
    {"id": 1, "display_name": "Admin", "email": "a@x", "avatar": ""},
    {"id": 7, "display_name": "Jess", "email": "j@x", "avatar": ""},
])

import api_server
api_server.DB_FILE = db.DB_FILE

from fastapi.testclient import TestClient
client = TestClient(api_server.app)

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

# Add a list assigned to a real user
r = client.post("/api/lists", json={"list_type": "imdb", "list_id": "ls123456789", "user_id": "7"})
check("add list status", r.status_code, 200)
check("add list user", r.json().get("user_id"), "7")
check("add list name", r.json().get("user_display_name"), "Jess")

# Adding with a user that doesn't exist is rejected up front
r = client.post("/api/lists", json={"list_type": "imdb", "list_id": "ls999", "user_id": "42"})
check("add unknown user rejected", r.status_code, 400)
print("      detail:", r.json().get("detail"))

# GET returns the assignment plus a resolved name
r = client.get("/api/lists")
row = next(l for l in r.json()["lists"] if l["list_id"] == "ls123456789")
check("get list user", row["user_id"], "7")
check("get list user name", row["user_display_name"], "Jess")

# Reassign by bare ID
r = client.patch("/api/lists/imdb/ls123456789/user", json={"user_id": "1"})
check("reassign status", r.status_code, 200)
check("reassign user", r.json()["user_id"], "1")
check("reassign persisted", db.get_list_user_id("imdb", "ls123456789"), "1")

# Reassign a list stored as a bare ID, addressed by its full URL
r = client.patch("/api/lists/imdb/https://www.imdb.com/list/ls123456789/user", json={"user_id": "7"})
check("reassign by url status", r.status_code, 200)
check("reassign by url persisted", db.get_list_user_id("imdb", "ls123456789"), "7")

# Bad inputs
r = client.patch("/api/lists/imdb/ls000/user", json={"user_id": "7"})
check("reassign missing list", r.status_code, 404)
r = client.patch("/api/lists/imdb/ls123456789/user", json={"user_id": "42"})
check("reassign unknown user", r.status_code, 400)
r = client.patch("/api/lists/imdb/ls123456789/user", json={"user_id": "  "})
check("reassign blank user", r.status_code, 400)
check("blank user left assignment alone", db.get_list_user_id("imdb", "ls123456789"), "7")

# A list stored as a URL is reachable both ways too
db.save_list_id("https://www.imdb.com/list/ls555000111/", "imdb", user_id="1")
r = client.patch("/api/lists/imdb/ls555000111/user", json={"user_id": "7"})
check("url-stored list reassigned by id", r.status_code, 200)
check("url-stored list persisted", db.get_list_user_id("imdb", "https://www.imdb.com/list/ls555000111"), "7")

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
