"""Check SeerrClient classifies each Seerr failure mode correctly."""
import os, sys, types, logging

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for name in ("seleniumbase", "bs4", "dotenv"):
    try:
        __import__(name)
    except ImportError:
        mod = types.ModuleType(name)
        for attr in ("SB", "BeautifulSoup", "load_dotenv", "set_key", "find_dotenv"):
            setattr(mod, attr, lambda *a, **k: None)
        sys.modules[name] = mod

import requests
from list_sync.api.seerr import SeerrClient

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        fail.append(label)

class FakeResponse:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text
    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

captured = {}
def make_post(response):
    def _post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return response
    return _post

client = SeerrClient("https://seerr.example.com/", "KEY", "1")

# The requester must travel as X-Api-User, not as the client default.
requests.post = make_post(FakeResponse(201, {"id": 5}))
check("success", client.request_media(603, "movie", requester_user_id="7"), "success")
check("X-Api-User header sent", captured["headers"].get("X-Api-User"), "7")
check("url has no double slash", captured["url"], "https://seerr.example.com/api/v1/request")
check("payload", captured["json"], {"mediaId": 603, "mediaType": "movie", "is4k": False})

# 409 is Overseerr's real duplicate response - the old code called this an error.
requests.post = make_post(FakeResponse(409, {"message": "Request for this media already exists."}))
check("409 duplicate", client.request_media(603, "movie", requester_user_id="7"), "already_requested")

# 403 splits into two very different problems.
requests.post = make_post(FakeResponse(403, {"message": "Movie Quota exceeded."}))
check("403 quota", client.request_media(603, "movie", requester_user_id="7"), "error")

requests.post = make_post(FakeResponse(403, {"message": "You do not have permission to make movie requests."}))
check("403 permission", client.request_media(603, "movie", requester_user_id="7"), "error")

# 401 means the user id doesn't exist on the server.
requests.post = make_post(FakeResponse(401, {"message": "You do not have permission to access this endpoint"}))
check("401 unknown user", client.request_media(603, "movie", requester_user_id="99"), "error")

# Legacy 400-duplicate still recognised; a real 400 is still an error.
requests.post = make_post(FakeResponse(400, {"message": "Request already exists"}))
check("400 duplicate", client.request_media(603, "movie"), "already_requested")
requests.post = make_post(FakeResponse(400, {"message": "Invalid media id"}))
check("400 real error", client.request_media(603, "movie"), "error")

# Non-JSON error body must not blow up.
requests.post = make_post(FakeResponse(500, None, text="<html>nginx</html>"))
check("500 html body", client.request_media(603, "movie"), "error")

# Network failure.
def boom(*a, **k):
    raise requests.exceptions.ConnectionError("refused")
requests.post = boom
check("connection error", client.request_media(603, "movie"), "error")

# TV paths route through the same helper.
requests.post = make_post(FakeResponse(201, {"id": 1}))
check("tv series", client.request_tv_series(1399, 3, requester_user_id="4"), "success")
check("tv seasons payload", captured["json"]["seasons"], [1, 2, 3])
check("tv user header", captured["headers"].get("X-Api-User"), "4")
check("specific season", client.request_specific_season(1399, 2, requester_user_id="4"), "success")
check("season payload", captured["json"]["seasons"], [2])

# --- validate_requester ---
USERS = {"results": [
    {"id": 1, "displayName": "Admin", "permissions": 2},
    {"id": 7, "displayName": "Jess", "permissions": 32},
    {"id": 8, "displayName": "NoRequest", "permissions": 64},
    {"id": 9, "displayName": "MovieOnly", "permissions": 262144},
]}
def fake_get(url, headers=None, params=None, timeout=None):
    return FakeResponse(200, USERS)
requests.get = fake_get

check("admin valid", client.validate_requester("1")[0], True)
check("request perm valid", client.validate_requester("7")[0], True)
check("movie-only perm valid", client.validate_requester("9")[0], True)
check("no request perm", client.validate_requester("8")[0], False)
print("      reason:", client.validate_requester("8")[1])
check("unknown user", client.validate_requester("42")[0], False)
print("      reason:", client.validate_requester("42")[1])

# A failed user lookup must not block the sync.
def failing_get(*a, **k):
    raise requests.exceptions.ConnectionError("refused")
requests.get = failing_get
check("lookup failure is non-fatal", client.validate_requester("7")[0], True)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
