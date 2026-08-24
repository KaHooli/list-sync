"""Check per-user request fan-out and the requester extraction."""
import sys, types

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
# cryptography's rust bindings are broken in this container and fail with a
# pyo3 PanicException (a BaseException), so stub it unconditionally.
c = stub("cryptography"); f = stub("cryptography.fernet", ("Fernet", "InvalidToken")); c.fernet = f
d = stub("dotenv"); d.load_dotenv = lambda *a, **k: None; d.set_key = lambda *a, **k: None

from list_sync.api.overseerr import OverseerrClient

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

# --- collect_request_user_ids ---
from list_sync.main import collect_request_user_ids as collect

check("single user", collect([{"type": "imdb", "id": "a", "user_id": "7"}], "1"), ["7"])
check("two users", collect([
    {"type": "imdb", "id": "a", "user_id": "7"},
    {"type": "imdb", "id": "b", "user_id": "3"},
], "1"), ["7", "3"])
check("dedups repeats", collect([
    {"type": "imdb", "id": "a", "user_id": "7"},
    {"type": "imdb", "id": "b", "user_id": "7"},
], "1"), ["7"])
check("missing user falls back to default", collect([{"type": "imdb", "id": "a"}], "4"), ["4"])
check("int user ids coerced", collect([{"type": "imdb", "id": "a", "user_id": 7}], "1"), ["7"])
check("empty source lists", collect([], "9"), ["9"])
check("mixed assigned and default", collect([
    {"type": "imdb", "id": "a", "user_id": "7"},
    {"type": "trakt", "id": "b"},
], "1"), ["7", "1"])

# --- _extract_requester_ids ---
ex = OverseerrClient._extract_requester_ids
media_info = {"requests": [
    {"is4k": False, "requestedBy": {"id": 7}},
    {"is4k": False, "requestedBy": {"id": 3}},
    {"is4k": True,  "requestedBy": {"id": 9}},
]}
check("non-4k requesters", ex(media_info, False), {"7", "3"})
check("4k requesters", ex(media_info, True), {"9"})
check("no requests key", ex({}, False), set())
check("null requests", ex({"requests": None}, False), set())
check("malformed entries ignored", ex({"requests": [None, "x", {"requestedBy": None}]}, False), set())
check("missing is4k treated as non-4k", ex({"requests": [{"requestedBy": {"id": 5}}]}, False), {"5"})

# --- get_media_state end to end ---
import requests as rq
class R:
    def __init__(self, body): self._b = body
    def json(self): return self._b
    def raise_for_status(self): pass

client = OverseerrClient("https://seerr.example.com", "KEY", "1")

rq.get = lambda *a, **k: R({"mediaInfo": {"status": 5, "requests": [{"is4k": False, "requestedBy": {"id": 1}}]}})
s = client.get_media_state(603, "movie")
check("available", (s["is_available"], s["is_requested"]), (True, False))
check("available requesters", s["requested_by_user_ids"], {"1"})

rq.get = lambda *a, **k: R({"mediaInfo": {"status": 2, "requests": [{"is4k": False, "requestedBy": {"id": 1}}]}})
s = client.get_media_state(603, "movie")
check("pending", (s["is_available"], s["is_requested"]), (False, True))

rq.get = lambda *a, **k: R({"mediaInfo": None})
s = client.get_media_state(603, "movie")
check("not in overseerr", (s["is_available"], s["is_requested"], s["requested_by_user_ids"]), (False, False, set()))

rq.get = lambda *a, **k: R({"mediaInfo": {"status": 0}})
s = client.get_media_state(603, "movie")
check("status 0", (s["is_available"], s["is_requested"]), (False, False))

rq.get = lambda *a, **k: R({"numberOfSeasons": 4, "mediaInfo": {"status": 0}})
check("seasons", client.get_media_state(1399, "tv")["number_of_seasons"], 4)
rq.get = lambda *a, **k: R({"mediaInfo": {"status": 0}})
check("seasons default", client.get_media_state(1399, "tv")["number_of_seasons"], 1)

# back-compat wrapper still returns the 3-tuple
rq.get = lambda *a, **k: R({"mediaInfo": {"status": 5}, "numberOfSeasons": 2})
check("legacy tuple", client.get_media_status(603, "movie"), (True, False, 2))

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
