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

# --- book lists are gated on what the connected Seerr can request ----------
def with_book_support(**books):
    """Pin the capability answer the endpoints read, as the probe would."""
    answer = {"supported": True, "ebook": True, "audiobook": True,
              "known": True, "reason": "Book requests are available."}
    answer.update(books)
    api_server._capabilities_cache = (float("inf"), {"books": answer})

# No book support: the list is refused rather than stored to fail every sync.
with_book_support(supported=False, ebook=False, audiobook=False,
                  reason="This Seerr server has no book support.")
r = client.post("/api/lists", json={"list_type": "goodreads", "list_id": "19281606:to-read",
                                    "user_id": "7"})
check("book list refused without support", r.status_code, 400)
check("nothing stored", db.get_list_book_format("goodreads", "19281606:to-read"), None)

caps = client.get("/api/system/capabilities").json()
check("capabilities report no formats", caps["books"]["formats"], [])
check("capabilities name the book providers", caps["books"]["providers"],
      ["goodreads", "openlibrary"])

# Audiobooks only: an ebook list is refused, an audiobook list is stored.
with_book_support(ebook=False, reason="No default ebook Bookshelf server.")
r = client.post("/api/lists", json={"list_type": "goodreads", "list_id": "19281606:to-read",
                                    "user_id": "7", "book_format": "ebook"})
check("unavailable format refused", r.status_code, 400)

r = client.post("/api/lists", json={"list_type": "goodreads", "list_id": "19281606:to-read",
                                    "user_id": "7", "book_format": "audiobook"})
check("available format accepted", r.status_code, 200)
check("format returned", r.json()["book_format"], "audiobook")
check("format persisted", db.get_list_book_format("goodreads", "19281606:to-read"), "audiobook")

caps = client.get("/api/system/capabilities").json()
check("only the usable format is offered", caps["books"]["formats"], ["audiobook"])

row = next(l for l in client.get("/api/lists").json()["lists"]
           if l["list_id"] == "19281606:to-read")
check("list reports its format", row["book_format"], "audiobook")
check("movie lists report no format",
      next(l for l in client.get("/api/lists").json()["lists"]
           if l["list_id"] == "ls123456789")["book_format"], None)

# Changing the format goes through the same capability check.
r = client.patch("/api/lists/goodreads/19281606:to-read/book-format", json={"book_format": "ebook"})
check("switch to unavailable format refused", r.status_code, 400)

with_book_support()
r = client.patch("/api/lists/goodreads/19281606:to-read/book-format", json={"book_format": "both"})
check("switch format", r.status_code, 200)
check("switch persisted", db.get_list_book_format("goodreads", "19281606:to-read"), "both")

r = client.patch("/api/lists/goodreads/19281606:to-read/book-format", json={"book_format": "paperback"})
check("unknown format refused", r.status_code, 400)
r = client.patch("/api/lists/imdb/ls123456789/book-format", json={"book_format": "ebook"})
check("format refused on a movie list", r.status_code, 400)
r = client.patch("/api/lists/goodreads/42:read/book-format", json={"book_format": "ebook"})
check("format on a missing list", r.status_code, 404)

# A book list added without a format takes the documented default.
r = client.post("/api/lists", json={"list_type": "openlibrary", "list_id": "jane/OL123L",
                                    "user_id": "7"})
check("default format applied", r.json()["book_format"], "ebook")

# An inconclusive probe - Seerr unreachable, or a key without admin - is not
# evidence that books are unsupported, so it must not block the list. Refusing
# on it would hide a provider that works perfectly well.
with_book_support(supported=False, ebook=False, audiobook=False, known=False,
                  reason="Could not reach Seerr to check for book support.")
r = client.post("/api/lists", json={"list_type": "goodreads", "list_id": "77:to-read",
                                    "user_id": "7", "book_format": "audiobook"})
check("unknown support does not block the list", r.status_code, 200)
check("requested format kept", r.json()["book_format"], "audiobook")

r = client.patch("/api/lists/goodreads/77:to-read/book-format", json={"book_format": "both"})
check("unknown support does not block a format change", r.status_code, 200)

caps = client.get("/api/system/capabilities").json()
check("unknown support reports no confirmed formats", caps["books"]["formats"], [])
check("unknown support is flagged as unsettled", caps["books"]["known"], False)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
