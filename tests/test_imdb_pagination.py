"""
Reproduce the runaway IMDb pagination loop and prove the fix terminates.

Scenario A is the live failure: an IMDb watchlist where the total-item count
can't be parsed, there is no pagination widget, and ?page=N re-serves the same
titles forever. Before the fix this never returns.

Scenario B proves ordinary multi-page lists still paginate to completion.
"""
import signal
import sys
import types

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

import logging
logging.disable(logging.CRITICAL)

from list_sync.providers.imdb import _process_imdb_list

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)


class FakeElement:
    """A list row, or a title/link element inside one."""
    def __init__(self, title="", href="", text=""):
        self._title = title
        self._href = href
        self.text = text or title

    def find_element(self, _by, selector):
        if selector in (".ipc-title__text", "h3.ipc-title__text",
                        "a.ipc-title-link-wrapper h3", ".dli-title h3"):
            return FakeElement(text=self._title)
        if "a" in selector or "link" in selector:
            return FakeElement(href=self._href)
        raise Exception(f"no element for {selector}")

    def find_elements(self, _by, selector):
        if selector == "a":
            return [FakeElement(href=self._href)]
        return []

    def get_attribute(self, name):
        if name == "href":
            return self._href
        return None


def make_rows(specs):
    """specs: list of (title, imdb_id)."""
    return [
        FakeElement(title=t, href=f"https://www.imdb.com/title/{i}/")
        for t, i in specs
    ]


class FakeSB:
    """
    Minimal SeleniumBase stand-in.

    pages: list of row-lists returned in order as navigation happens.
    stuck: when True, navigation never advances - always serves pages[0].
    has_total: whether the total-items element is parseable.
    has_pagination: whether a next button exists.
    scroll_batches: when set, models infinite scroll - each scroll appends the
        next batch to what's rendered, as an IMDb watchlist does.
    """
    def __init__(self, pages, stuck=False, has_total=False, has_pagination=False,
                 scroll_batches=None):
        self.pages = pages
        self.stuck = stuck
        self.has_total = has_total
        self.has_pagination = has_pagination
        self.index = 0
        self.opens = 0
        self.scrolls = 0
        self.scroll_batches = scroll_batches
        self.rendered = list(scroll_batches[0]) if scroll_batches else None
        self._batch = 1

    # --- navigation -----------------------------------------------------
    def open(self, url):
        self.opens += 1
        if self.opens > 5000:
            raise AssertionError("runaway loop: over 5000 navigations")
        if not self.stuck:
            self.index = min(self.index + 1, len(self.pages) - 1)

    def sleep(self, _n):
        pass

    def wait_for_element_present(self, _sel, timeout=None):
        return True

    def execute_script(self, script, *_a):
        # Model infinite scroll: reaching the bottom appends the next batch.
        if self.scroll_batches and "scrollHeight" in str(script):
            self.scrolls += 1
            if self.scrolls > 500:
                raise AssertionError("runaway loop: over 500 scrolls")
            if self._batch < len(self.scroll_batches):
                self.rendered.extend(self.scroll_batches[self._batch])
                self._batch += 1
        return None

    # --- lookups --------------------------------------------------------
    def find_element(self, *args):
        selector = args[-1]
        if "total-items" in selector or "sc-d6269c7a-1" in selector:
            if self.has_total:
                return FakeElement(text=f"{sum(len(p) for p in self.pages)} titles")
            raise Exception("total items not found")
        if "pagination" in selector:
            if self.has_pagination:
                return FakeElement(text="next")
            raise Exception("no pagination widget")
        if selector == "ul.ipc-metadata-list":
            return _ListParent(self.pages[self.index])
        raise Exception(f"no element for {selector}")

    def find_elements(self, *args):
        selector = args[-1]
        if "ipc-metadata-list-summary-item" in selector:
            if self.rendered is not None:
                return self.rendered
            return self.pages[self.index]
        return []


class _ListParent:
    def __init__(self, rows):
        self._rows = rows
    def find_elements(self, _by, _selector):
        return self._rows


def with_timeout(seconds, fn, *args):
    """Run fn, failing loudly if it hangs rather than hanging the test run."""
    def handler(_s, _f):
        raise TimeoutError(f"did not terminate within {seconds}s")
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        return fn(*args), None
    except (TimeoutError, AssertionError) as e:
        return None, str(e)
    finally:
        signal.alarm(0)


# --- Scenario A: the live watchlist failure -----------------------------
print("Scenario A: watchlist, no total count, no pagination, ?page=N stuck\n")

watchlist_rows = make_rows([
    ("1. Spider-Man", "tt0145487"),
    ("2. Spider-Man 2", "tt0316654"),
    ("3. Spider-Man 3", "tt0413300"),
    ("4. The Amazing Spider-Man", "tt0948470"),
    ("5. Spider-Man: Homecoming", "tt2250912"),
    ("6. Spider-Man: No Way Home", "tt10872600"),
    ("7. Stranger Things", "tt4574334"),
])

sb = FakeSB([watchlist_rows], stuck=True, has_total=False, has_pagination=False)
result, error = with_timeout(20, _process_imdb_list, sb, "https://www.imdb.com/user/ur171928620/watchlist")

check("A terminates", error, None)
if result is not None:
    check("A returns the 7 unique titles", len(result), 7)
    check("A has no duplicates", len({r["imdb_id"] for r in result}), 7)
    check("A stopped navigating quickly", sb.opens <= 3, True)
    print(f"      navigations: {sb.opens}, titles: {[r['title'] for r in result][:3]}...")

# --- Scenario B: a healthy multi-page list ------------------------------
print("\nScenario B: three real pages that advance\n")

pages = [
    make_rows([("1. A", "tt001"), ("2. B", "tt002")]),
    make_rows([("3. C", "tt003"), ("4. D", "tt004")]),
    make_rows([("5. E", "tt005")]),
]
sb = FakeSB(pages, stuck=False, has_total=False, has_pagination=False)
result, error = with_timeout(20, _process_imdb_list, sb, "https://www.imdb.com/list/ls123")

check("B terminates", error, None)
if result is not None:
    check("B collects every page", len(result), 5)
    check("B preserves order", [r["imdb_id"] for r in result], ["tt001","tt002","tt003","tt004","tt005"])

# --- Scenario C: pages that repeat after a while ------------------------
print("\nScenario C: advances twice, then starts repeating\n")

repeat = make_rows([("1. A", "tt001"), ("2. B", "tt002")])
pages = [repeat, make_rows([("3. C", "tt003")]), repeat]
sb = FakeSB(pages, stuck=False, has_total=False, has_pagination=False)
result, error = with_timeout(20, _process_imdb_list, sb, "https://www.imdb.com/list/ls456")

check("C terminates", error, None)
if result is not None:
    check("C keeps the distinct titles", sorted(r["imdb_id"] for r in result), ["tt001","tt002","tt003"])


# --- Scenario D: a real watchlist — infinite scroll, no pagination widget ---
print("\nScenario D: watchlist loading 7 + 100 + 93 titles by scrolling\n")

batches = [
    make_rows([(f"{i}. Title {i}", f"tt{i:07d}") for i in range(1, 8)]),
    make_rows([(f"{i}. Title {i}", f"tt{i:07d}") for i in range(8, 108)]),
    make_rows([(f"{i}. Title {i}", f"tt{i:07d}") for i in range(108, 201)]),
]
sb = FakeSB([[]], stuck=True, has_total=False, has_pagination=False,
            scroll_batches=batches)
result, error = with_timeout(30, _process_imdb_list, sb,
                             "https://www.imdb.com/user/ur171928620/watchlist")

check("D terminates", error, None)
if result is not None:
    check("D collects the WHOLE list, not just the first batch", len(result), 200)
    check("D no duplicates", len({r["imdb_id"] for r in result}), 200)
    check("D first title", result[0]["imdb_id"], "tt0000001")
    check("D last title", result[-1]["imdb_id"], "tt0000200")
    # Scrolling does the work; ?page=N is only a backstop tried once after
    # scrolling is exhausted, and must not restart the loop.
    check("D used scrolling, not page-walking", sb.opens <= 1, True)
    print(f"      scrolls: {sb.scrolls}, page-navigations: {sb.opens}")

# --- Scenario E: scrolling stops helping -> stop, don't spin ---------------
print("\nScenario E: scroll yields nothing new\n")

sb = FakeSB([[]], stuck=True, has_total=False, has_pagination=False,
            scroll_batches=[make_rows([("1. Only", "tt0000001")])])
result, error = with_timeout(30, _process_imdb_list, sb,
                             "https://www.imdb.com/user/ur1/watchlist")
check("E terminates", error, None)
if result is not None:
    check("E keeps the one title", len(result), 1)
    check("E gave up scrolling promptly", sb.scrolls <= 5, True)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
