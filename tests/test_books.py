"""Book lists: capability probe, shelf parsing, format handling and requests."""
import sys, types, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stub(name, attrs=()):
    m = types.ModuleType(name)
    for a in attrs:
        setattr(m, a, type(a, (), {}))
    sys.modules[name] = m
    return m


for n in ("seleniumbase", "bs4", "halo", "discord_webhook"):
    try: __import__(n)
    except ImportError: stub(n, ("SB", "BeautifulSoup", "Halo", "DiscordWebhook", "DiscordEmbed"))
c = stub("cryptography"); f = stub("cryptography.fernet", ("Fernet", "InvalidToken")); c.fernet = f
d = stub("dotenv"); d.load_dotenv = lambda *a, **k: None; d.set_key = lambda *a, **k: None

import requests as rq

from list_sync.api.seerr import SeerrClient
from list_sync.books import format_label, is_book_provider, normalize_book_format
from list_sync.providers.goodreads import (
    clean_book_title,
    parse_goodreads_list_id,
    _parse_year,
)
from list_sync.providers.openlibrary import (
    _item_from_reading_log,
    _item_from_seed,
    parse_openlibrary_list_id,
)

fail = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)


def raises(label, fn, *args):
    try:
        fn(*args)
    except ValueError:
        print(f"PASS  {label}")
        return
    print(f"FAIL  {label}: expected ValueError")
    fail.append(label)


class Response:
    """Minimal stand-in for a requests response."""
    def __init__(self, body=None, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = rq.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


# --- format vocabulary ----------------------------------------------------
check("format passthrough", normalize_book_format("audiobook"), "audiobook")
check("format alias", normalize_book_format("Audiobooks"), "audiobook")
check("format 'all' means both", normalize_book_format("all"), "both")
check("format default on junk", normalize_book_format("paperback"), "ebook")
check("format default is honoured", normalize_book_format(None, "both"), "both")
check("format label", format_label("both"), "Audiobooks + eBooks")
check("goodreads is a book list", is_book_provider("goodreads"), True)
check("openlibrary is a book list", is_book_provider("OpenLibrary"), True)
check("imdb is not a book list", is_book_provider("imdb"), False)

# --- Goodreads shelf identifiers ------------------------------------------
check("bare user id", parse_goodreads_list_id("19281606"), ("19281606", "to-read"))
check("user id and shelf", parse_goodreads_list_id("19281606:read"), ("19281606", "read"))
check("profile id with slug", parse_goodreads_list_id("19281606-jane"), ("19281606", "to-read"))
check("shelf url", parse_goodreads_list_id(
    "https://www.goodreads.com/review/list/19281606?shelf=science-fiction"),
    ("19281606", "science-fiction"))
check("profile url", parse_goodreads_list_id(
    "https://www.goodreads.com/user/show/19281606-jane"), ("19281606", "to-read"))
raises("rejects non-Goodreads id", parse_goodreads_list_id, "jane")
raises("rejects empty id", parse_goodreads_list_id, "")

check("series suffix stripped", clean_book_title("Dune (Dune, #1)"), "Dune")
check("range suffix stripped", clean_book_title("Book (Series, #1-3)"), "Book")
check("plain title kept", clean_book_title("1984"), "1984")
check("unrelated parens kept", clean_book_title("Book (Illustrated)"), "Book (Illustrated)")
check("year parsed", _parse_year("1965"), 1965)
check("year from date", _parse_year("1965-06-01"), 1965)
check("junk year ignored", _parse_year("unknown"), None)

# --- Open Library identifiers ---------------------------------------------
check("ol list", parse_openlibrary_list_id("jane/OL123L"), ("list", "jane", "OL123L"))
check("ol shelf alias", parse_openlibrary_list_id("jane:to-read"), ("shelf", "jane", "want-to-read"))
check("ol list url", parse_openlibrary_list_id(
    "https://openlibrary.org/people/jane/lists/OL123L"), ("list", "jane", "OL123L"))
check("ol shelf url", parse_openlibrary_list_id(
    "https://openlibrary.org/people/jane/books/already-read"), ("shelf", "jane", "already-read"))
raises("rejects unknown ol shelf", parse_openlibrary_list_id, "jane:favourites")

check("seed becomes a book", _item_from_seed({"url": "/works/OL27448W", "title": "Dune"}), {
    "title": "Dune", "media_type": "book", "year": None, "author": None,
    "openlibrary_id": "OL27448W", "openlibrary_edition_id": None,
})
check("subject seed skipped", _item_from_seed({"url": "/subjects/fiction", "title": "Fiction"}), None)
check("reading log entry", _item_from_reading_log({
    "work": {"key": "/works/OL27448W", "title": "Dune", "author_names": ["Frank Herbert"],
             "author_keys": ["/authors/OL79034A"], "first_publish_year": 1965},
    "logged_edition": "/books/OL8934157M",
}), {
    "title": "Dune", "media_type": "book", "year": 1965, "author": "Frank Herbert",
    "openlibrary_id": "OL27448W", "openlibrary_edition_id": "OL8934157M",
    "openlibrary_author_id": "OL79034A",
})

# --- capability probe ------------------------------------------------------
client = SeerrClient("https://seerr.example.com", "KEY", "1")

rq.get = lambda *a, **k: Response(status_code=404)
books = client.get_capabilities(refresh=True)["books"]
check("no book endpoint means no books", (books["supported"], books["known"]), (False, True))

rq.get = lambda *a, **k: Response([
    {"id": 0, "isDefault": True, "serviceType": "ebook"},
    {"id": 1, "isDefault": True, "serviceType": "audiobook"},
])
books = client.get_capabilities(refresh=True)["books"]
check("both formats", (books["supported"], books["ebook"], books["audiobook"]), (True, True, True))
check("capabilities are cached", client.get_capabilities()["books"] is books, True)
check("supports_books", client.supports_books(), True)

# A service with no serviceType is an ebook service, matching Seerr's default.
rq.get = lambda *a, **k: Response([{"id": 0, "isDefault": True}])
books = client.get_capabilities(refresh=True)["books"]
check("untyped service is ebook", (books["ebook"], books["audiobook"]), (True, False))

# Configured but not default: Seerr would refuse the request, so neither counts.
rq.get = lambda *a, **k: Response([{"id": 0, "isDefault": False, "serviceType": "audiobook"}])
books = client.get_capabilities(refresh=True)["books"]
check("non-default service", (books["supported"], books["audiobook"]), (True, False))

rq.get = lambda *a, **k: Response(status_code=403)
books = client.get_capabilities(refresh=True)["books"]
check("rejected key is not a verdict", (books["supported"], books["known"]), (False, False))

# The Bookshelf settings route is newer than book support itself. When it is
# missing, the book endpoint - not the absent setting - decides the verdict.
def probe(settings_status, book_status):
    """Answer the settings probe and the /book/search fallback separately."""
    def get(url, *a, **k):
        if "/settings/readarr" in url:
            return Response(status_code=settings_status)
        if "/book/search" in url:
            return Response({"results": []} if book_status == 200 else None,
                            status_code=book_status)
        raise AssertionError(f"unexpected probe URL: {url}")
    return get

rq.get = probe(404, 200)
books = client.get_capabilities(refresh=True)["books"]
check("books found despite missing settings route",
      (books["supported"], books["known"]), (True, False))
check("formats left unverified rather than guessed",
      (books["ebook"], books["audiobook"]), (True, True))

rq.get = probe(404, 404)
books = client.get_capabilities(refresh=True)["books"]
check("no settings route and no book endpoint is a real no",
      (books["supported"], books["known"]), (False, True))

rq.get = probe(404, 500)
books = client.get_capabilities(refresh=True)["books"]
check("a broken book endpoint settles nothing",
      (books["supported"], books["known"]), (False, False))

def unreachable(*a, **k):
    raise rq.exceptions.ConnectionError("no route to host")

rq.get = unreachable
books = client.get_capabilities(refresh=True)["books"]
check("unreachable server", (books["supported"], books["known"]), (False, False))

# --- per-format availability and requesters --------------------------------
state = SeerrClient.book_state_from_media_info

check("no media info", state(None, "ebook")["is_available"], False)
check("ebook linked", state({"externalServiceId": 12}, "ebook")["is_available"], True)
check("ebook linked, audiobook wanted",
      state({"externalServiceId": 12}, "audiobook")["is_available"], False)
check("audiobook linked", state({"audiobookExternalServiceId": 3}, "audiobook")["is_available"], True)
check("both blocked by either half",
      state({"externalServiceId": 12}, "both")["is_available"], True)
check("blocklisted", state({"status": 6}, "ebook")["is_blocklisted"], True)

requests_info = {"requests": [
    {"bookFormat": "audiobook", "requestedBy": {"id": 7}},
    {"bookFormat": None, "requestedBy": {"id": 3}},
]}
check("audiobook requester", state(requests_info, "audiobook")["requested_by_user_ids"], {"7"})
check("null format counts as ebook",
      state(requests_info, "ebook")["requested_by_user_ids"], {"3"})
check("both overlaps everything",
      state(requests_info, "both")["requested_by_user_ids"], {"7", "3"})
check("both request blocks an ebook one",
      state({"requests": [{"bookFormat": "both", "requestedBy": {"id": 9}}]},
            "ebook")["requested_by_user_ids"], {"9"})
# A request whose requester Seerr didn't return still blocks ours, so it is
# counted rather than dropped; entries that aren't requests at all are ignored.
check("malformed entries ignored",
      state({"requests": [None, "x"]}, "ebook")["requested_by_user_ids"], set())
check("requester-less request still counts",
      state({"requests": [{"requestedBy": None}]}, "ebook")["requested_by_user_ids"], {"unknown"})

# --- book search scoring ---------------------------------------------------
def search_response(results):
    return lambda *a, **k: Response({"results": results})

rq.get = search_response([
    {"id": "OL1W", "title": "Dune", "author": "Someone Else", "firstPublishYear": 2001},
    {"id": "OL27448W", "title": "Dune", "author": "Frank Herbert", "firstPublishYear": 1965},
])
match = client.search_book("Dune", author="Frank Herbert", year=1965)
check("author decides between same titles", match["id"], "OL27448W")
check("match carries the author id key", "author_id" in match, True)

rq.get = search_response([{"id": "OL9W", "title": "Something Different", "author": "Nobody"}])
check("weak match rejected", client.search_book("Dune", author="Frank Herbert"), None)

rq.get = search_response([])
check("no results", client.search_book("Dune"), None)

rq.get = lambda *a, **k: Response(status_code=404)
check("missing search endpoint", client.search_book("Dune"), None)

# ISBN searches take the top hit, since an ISBN names one edition.
seen_queries = []
def isbn_search(url, **kwargs):
    seen_queries.append(kwargs.get("params", {}).get("query"))
    return Response({"results": [{"id": "OL27448W", "title": "Dune"}]})

rq.get = isbn_search
match = client.search_book("Dune (Dune, #1)", author="Frank Herbert", isbn="0-441-47812-3")
check("isbn match", match["id"], "OL27448W")
check("isbn query is a field query", seen_queries[0], "isbn:0441478123")

# --- request payloads ------------------------------------------------------
posted = {}
def capture_post(url, headers=None, json=None, timeout=None):
    posted["url"] = url
    posted["headers"] = headers
    posted["json"] = json
    return Response({}, status_code=201)

rq.post = capture_post
status = client.request_book("OL27448W", book_format="both", edition_id="OL8934157M",
                             author_id="OL79034A", isbn13="9780441478125", requester_user_id="7")
check("request succeeds", status, "success")
check("request payload", posted["json"], {
    "mediaType": "book", "mediaId": "OL27448W", "format": "both",
    "editionId": "OL8934157M", "authorId": "OL79034A", "isbn13": "9780441478125",
})
check("requests as the list's user", posted["headers"]["X-Api-User"], "7")

def conflict_post(*a, **k):
    return Response({"message": "Request for this book already exists."}, status_code=409)

rq.post = conflict_post
check("duplicate is not an error", client.request_book("OL27448W"), "already_requested")

rq.post = capture_post
client.request_book("OL27448W")
check("optional ids omitted", posted["json"],
      {"mediaType": "book", "mediaId": "OL27448W", "format": "ebook"})
check("empty id refused", client.request_book(""), "error")

# --- per (user, format) fan-out --------------------------------------------
from list_sync.main import collect_book_requests as collect

check("one shelf", collect([{"type": "goodreads", "id": "a", "user_id": "7",
                             "book_format": "audiobook"}], "1"), [("7", "audiobook")])
check("same book, two people", collect([
    {"type": "goodreads", "id": "a", "user_id": "7", "book_format": "ebook"},
    {"type": "goodreads", "id": "b", "user_id": "3", "book_format": "ebook"},
], "1"), [("7", "ebook"), ("3", "ebook")])
check("same person, two formats", collect([
    {"type": "goodreads", "id": "a", "user_id": "7", "book_format": "ebook"},
    {"type": "goodreads", "id": "b", "user_id": "7", "book_format": "audiobook"},
], "1"), [("7", "ebook"), ("7", "audiobook")])
check("duplicates collapse", collect([
    {"type": "goodreads", "id": "a", "user_id": "7", "book_format": "ebook"},
    {"type": "goodreads", "id": "b", "user_id": "7", "book_format": "ebook"},
], "1"), [("7", "ebook")])
check("falls back to defaults", collect([{"type": "goodreads", "id": "a"}], "4", "both"),
      [("4", "both")])
check("no source lists", collect([], "9"), [("9", "ebook")])

# --- database round trip: per-list format, book identity -------------------
import tempfile

tmp = tempfile.mkdtemp()
import list_sync.utils.logger as lg
lg.DATA_DIR = tmp
import list_sync.database as db
db.DB_FILE = os.path.join(tmp, "list_sync.db")
db.init_database()

db.save_list_id("19281606:to-read", "goodreads", user_id="7", book_format="audiobook")
db.save_list_id("ls123456789", "imdb", user_id="7")

check("book format stored", db.get_list_book_format("goodreads", "19281606:to-read"), "audiobook")
check("format found via url", db.get_list_book_format(
    "goodreads", "https://www.goodreads.com/review/list/19281606?shelf=to-read"), "audiobook")
check("movie lists have no format", db.get_list_book_format("imdb", "ls123456789"), None)

stored = {(l["type"], l["id"]): l for l in db.load_list_ids()}
check("book list reports its format",
      stored[("goodreads", "19281606:to-read")]["book_format"], "audiobook")
check("movie list carries no format key",
      "book_format" in stored[("imdb", "ls123456789")], False)

# Re-saving without a format must not reset the one already chosen.
db.save_list_id("19281606:to-read", "goodreads", item_count=12)
check("format survives a re-save", db.get_list_book_format("goodreads", "19281606:to-read"), "audiobook")

check("format can be changed",
      db.update_list_book_format("goodreads", "19281606:to-read", "both"), True)
check("changed format stored", db.get_list_book_format("goodreads", "19281606:to-read"), "both")
check("refuses a non-book list", db.update_list_book_format("imdb", "ls123456789", "both"), False)

# A new book list defaults to the documented format rather than to nothing.
db.save_list_id("jane/OL123L", "openlibrary")
check("default format applied", db.get_list_book_format("openlibrary", "jane/OL123L"), "ebook")

# Books are matched between syncs on their Open Library ID, not a numeric one.
db.save_sync_result("Dune", "book", None, None, "requested", 1965, None,
                    "goodreads", "19281606:to-read", external_id="OL27448W")
check("book is not resynced inside the window", db.should_sync_item(None, external_id="OL27448W"), False)
check("other books still sync", db.should_sync_item(None, external_id="OL999W"), True)
check("no id at all still syncs", db.should_sync_item(None), True)

db.save_sync_result("Dune", "book", None, None, "already_available", 1965, None,
                    "goodreads", "19281606:to-read", external_id="OL27448W")
with db.sqlite3.connect(db.DB_FILE) as conn:
    rows = conn.execute(
        "SELECT title, status FROM synced_items WHERE external_id = 'OL27448W'"
    ).fetchall()
check("book row is updated, not duplicated", rows, [("Dune", "already_available")])

# --- env configuration -----------------------------------------------------
import list_sync.config as cfg
from list_sync.config import parse_book_list_entry

check("bare book entry", parse_book_list_entry("19281606"), ("19281606", None, None))
check("entry with user", parse_book_list_entry("19281606:to-read::7"),
      ("19281606:to-read", "7", None))
check("entry with format", parse_book_list_entry("19281606:to-read|audiobook"),
      ("19281606:to-read", None, "audiobook"))
check("entry with user and format", parse_book_list_entry("19281606:to-read|both::7"),
      ("19281606:to-read", "7", "both"))
check("unknown format left in the id", parse_book_list_entry("jane|paperback"),
      ("jane|paperback", None, None))

class BrokenConfigManager(Exception):
    pass

cfg.ConfigManager = lambda *a, **k: (_ for _ in ()).throw(BrokenConfigManager())

os.environ["OVERSEERR_USER_ID"] = "4"
os.environ["BOOK_FORMAT"] = "both"
os.environ["GOODREADS_LISTS"] = "19281606:to-read|audiobook::7, 42:read"
os.environ["OPENLIBRARY_LISTS"] = "jane/OL987L"
cfg.load_env_lists()

check("env book list user", db.get_list_user_id("goodreads", "19281606:to-read"), "7")
check("env book list format", db.get_list_book_format("goodreads", "19281606:to-read"), "audiobook")
check("env falls back to BOOK_FORMAT", db.get_list_book_format("goodreads", "42:read"), "both")
check("env open library list", db.get_list_user_id("openlibrary", "jane/OL987L"), "4")

before = len(db.load_list_ids())
cfg.load_env_lists()
check("re-running adds nothing", len(db.load_list_ids()), before)

os.environ["GOODREADS_LISTS"] = "19281606:to-read|ebook::7, 42:read"
cfg.load_env_lists()
check("env change reassigns the format",
      db.get_list_book_format("goodreads", "19281606:to-read"), "ebook")
check("still no duplicates", len(db.load_list_ids()), before)

# --- end to end: a shelf's format reaches the request it produces ----------
import list_sync.main as main
import list_sync.providers as providers

db.save_list_id("55:sci-fi", "goodreads", user_id="3", book_format="audiobook")

# A fake provider stands in for the shelf, so the wiring is what's under test
# rather than Goodreads' feed.
providers.PROVIDERS["goodreads"] = lambda list_id: [
    {"title": "Dune", "media_type": "book", "year": 1965, "author": "Frank Herbert",
     "isbn": "0441478123"},
]

book_lists = [l for l in db.load_list_ids() if l["type"] == "goodreads" and l["id"] == "55:sci-fi"]
items, synced = main.fetch_media_from_lists(book_lists)
check("shelf fetched one book", len(items), 1)
check("format rides along with the item", items[0]["_source_list_book_format"], "audiobook")
check("synced list carries its format", synced[0]["book_format"], "audiobook")

sources = main.get_source_lists_from_item(items[0])
check("source list keeps the format", sources[0]["book_format"], "audiobook")
check("source list keeps the user", sources[0]["user_id"], "3")


class FakeSeerr:
    """Records what a sync would ask Seerr for, without asking it."""
    requester_user_id = "1"
    book_state_from_media_info = staticmethod(SeerrClient.book_state_from_media_info)

    def __init__(self, book_id="OL45883W", **books):
        self.books = {"supported": True, "ebook": True, "audiobook": True,
                      "known": True, "reason": "ok", **books}
        self.book_id = book_id
        self.requested = []

    def get_capabilities(self, refresh=False):
        return {"books": self.books}

    def get_book(self, book_id):
        return None

    def search_book(self, title, author=None, year=None, isbn=None):
        return {"id": self.book_id, "title": title, "author": author, "author_id": "OL79034A",
                "isbn13": "9780441478125", "edition_id": "OL8934157M", "media_info": {}}

    def request_book(self, book_id, book_format="ebook", **kwargs):
        self.requested.append((book_id, book_format, kwargs.get("requester_user_id")))
        return "success"


# A book synced inside the skip window is left alone - OL27448W was written by
# the database checks above.
seerr = FakeSeerr(book_id="OL27448W")
check("recently synced book is skipped",
      main.process_media_item(items[0], seerr, dry_run=False)["status"], "skipped")
check("skipped book is not requested", seerr.requested, [])

seerr = FakeSeerr()
result = main.process_media_item(items[0], seerr, dry_run=False)
check("book routed to the book path", result["media_type"], "book")
check("book requested", result["status"], "requested")
check("requested as the shelf's user, in its format", seerr.requested,
      [("OL45883W", "audiobook", "3")])

# The same book on two shelves is one request per (user, format).
two_shelves = dict(items[0])
two_shelves["_source_lists"] = [
    {"type": "goodreads", "id": "55:sci-fi", "user_id": "3", "book_format": "audiobook"},
    {"type": "goodreads", "id": "19281606:to-read", "user_id": "7", "book_format": "ebook"},
]
# Each scenario needs its own book: a book synced by the previous one is
# inside the skip window and would be left alone.
seerr = FakeSeerr(book_id="OL10001W")
main.process_media_item(two_shelves, seerr, dry_run=False)
check("one request per user and format", seerr.requested,
      [("OL10001W", "audiobook", "3"), ("OL10001W", "ebook", "7")])

# A request just made is remembered, so the second shelf wanting the same
# format doesn't earn a 409 from Seerr.
same_format = dict(items[0])
same_format["_source_lists"] = [
    {"type": "goodreads", "id": "a", "user_id": "3", "book_format": "ebook"},
    {"type": "goodreads", "id": "b", "user_id": "7", "book_format": "ebook"},
]
seerr = FakeSeerr(book_id="OL10002W")
main.process_media_item(same_format, seerr, dry_run=False)
check("overlapping format requested once", seerr.requested,
      [("OL10002W", "ebook", "3")])

# Without book support the item errors out rather than being requested.
seerr = FakeSeerr(book_id="OL10003W", supported=False, ebook=False, audiobook=False,
                  reason="This Seerr server has no book support.")
result = main.process_media_item(items[0], seerr, dry_run=False)
check("no book support is an error", result["status"], "error")
check("nothing requested", seerr.requested, [])

print()
if fail:
    print(f"{len(fail)} check(s) failed: {', '.join(fail)}")
    sys.exit(1)
print("All book checks passed")
