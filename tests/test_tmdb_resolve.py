"""Check IMDb -> TMDB resolution via TMDB's free /find endpoint."""
import sys, types

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

import requests
import list_sync.api.tmdb as tmdb
import list_sync.config as cfg

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

class R:
    def __init__(self, status, body=None):
        self.status_code = status; self._b = body
    def json(self):
        if self._b is None: raise ValueError("no json")
        return self._b
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

captured = {}
def fake_get(url, params=None, timeout=None):
    captured["url"] = url; captured["params"] = params
    return fake_get.response
requests.get = fake_get

cfg.get_tmdb_api_key = lambda: "TESTKEY"

MOVIE = {"movie_results": [{"id": 278, "title": "The Shawshank Redemption"}],
         "tv_results": [], "person_results": []}
TV = {"movie_results": [], "tv_results": [{"id": 1396, "name": "Breaking Bad"}]}

# --- exact movie resolution ---
fake_get.response = R(200, MOVIE)
got = tmdb.resolve_imdb_id("tt0111161", "movie")
check("movie tmdb_id", got["tmdb_id"], 278)
check("movie type", got["media_type"], "movie")
check("movie title", got["title"], "The Shawshank Redemption")
check("uses /find endpoint", captured["url"], "https://api.themoviedb.org/3/find/tt0111161")
check("external_source set", captured["params"]["external_source"], "imdb_id")
check("api key sent", captured["params"]["api_key"], "TESTKEY")

# --- tv resolution ---
fake_get.response = R(200, TV)
got = tmdb.resolve_imdb_id("tt0903747", "tv")
check("tv tmdb_id", got["tmdb_id"], 1396)
check("tv type", got["media_type"], "tv")
check("tv title", got["title"], "Breaking Bad")

# --- IMDb says movie, TMDB has it as tv: fall through, report actual type ---
fake_get.response = R(200, TV)
got = tmdb.resolve_imdb_id("tt0903747", "movie")
check("cross-type falls through", got["tmdb_id"], 1396)
check("cross-type reports tv", got["media_type"], "tv")

# --- no match ---
fake_get.response = R(200, {"movie_results": [], "tv_results": []})
check("no match -> None", tmdb.resolve_imdb_id("tt9999999", "movie"), None)

# --- error handling ---
fake_get.response = R(401, {"status_message": "Invalid API key"})
check("401 -> None", tmdb.resolve_imdb_id("tt0111161", "movie"), None)
fake_get.response = R(500, None)
check("500 -> None", tmdb.resolve_imdb_id("tt0111161", "movie"), None)
fake_get.response = R(200, None)
check("bad json -> None", tmdb.resolve_imdb_id("tt0111161", "movie"), None)

def boom(*a, **k): raise requests.exceptions.ConnectionError("refused")
requests.get = boom
check("network error -> None", tmdb.resolve_imdb_id("tt0111161", "movie"), None)
requests.get = fake_get

# --- guards ---
fake_get.response = R(200, MOVIE)
check("rejects non-imdb id", tmdb.resolve_imdb_id("12345", "movie"), None)
check("rejects empty", tmdb.resolve_imdb_id("", "movie"), None)

# --- no api key configured: must be a clean no-op so Trakt still runs ---
cfg.get_tmdb_api_key = lambda: None
check("no key -> unavailable", tmdb.is_available(), False)
check("no key -> None", tmdb.resolve_imdb_id("tt0111161", "movie"), None)
cfg.get_tmdb_api_key = lambda: "TESTKEY"
check("key -> available", tmdb.is_available(), True)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
