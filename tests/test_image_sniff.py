"""sniff_image_type must match what imghdr.what() returned, without imghdr."""
import os, sys, types, tempfile

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

tmp = tempfile.mkdtemp()
import list_sync.utils.logger as lg
lg.DATA_DIR = tmp
import list_sync.database as db
db.DB_FILE = os.path.join(tmp, "list_sync.db")

from api_server import sniff_image_type

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

# Real headers, padded so length checks behave as they would on a real file.
PAD = b"\x00" * 64
CASES = [
    ("jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + PAD, "jpeg"),
    ("jpeg exif variant", b"\xff\xd8\xff\xe1\x00\x1cExif\x00" + PAD, "jpeg"),
    ("png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + PAD, "png"),
    ("gif87a", b"GIF87a\x01\x00\x01\x00" + PAD, "gif"),
    ("gif89a", b"GIF89a\x01\x00\x01\x00" + PAD, "gif"),
    ("webp", b"RIFF\x24\x00\x00\x00WEBPVP8 " + PAD, "webp"),
    ("bmp", b"BM\x36\x00\x00\x00\x00\x00" + PAD, "bmp"),
    ("tiff little-endian", b"II*\x00\x08\x00\x00\x00" + PAD, "tiff"),
    ("tiff big-endian", b"MM\x00*\x00\x00\x00\x08" + PAD, "tiff"),
]
for name, data, want in CASES:
    check(f"detects {name}", sniff_image_type(data), want)

# Anything unrecognised must return None so the Content-Type fallback runs.
check("html error page", sniff_image_type(b"<!DOCTYPE html><html>" + PAD), None)
check("json error body", sniff_image_type(b'{"error":"not found"}'), None)
check("empty", sniff_image_type(b""), None)
check("none", sniff_image_type(None), None)
check("short", sniff_image_type(b"\xff"), None)

# A RIFF container that is not WebP (e.g. a wav) is not an image.
check("riff but not webp", sniff_image_type(b"RIFF\x24\x00\x00\x00WAVEfmt " + PAD), None)
# Truncated RIFF must not read past the end.
check("truncated riff", sniff_image_type(b"RIFF\x24\x00"), None)

# The signature must be at the start, not merely present.
check("png magic later in the body",
      sniff_image_type(b"junk" + b"\x89PNG\r\n\x1a\n" + PAD), None)

# Cross-check against imghdr itself where it still exists, so the replacement
# is verified against the thing it replaces rather than against my assumptions.
try:
    import imghdr
except ImportError:
    print("SKIP  imghdr cross-check (removed in this Python)")
else:
    for name, data, _want in CASES:
        expected = imghdr.what(None, data)
        if expected is None:
            continue  # imghdr didn't know this one either
        check(f"matches imghdr for {name}", sniff_image_type(data), expected)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
