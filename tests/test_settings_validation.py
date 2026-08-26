"""
Settings written through the API get the same checks as the setup wizard.

The wizard refused a Seerr URL pointing at cloud metadata, a webhook that isn't
on Discord, an out-of-range sync interval and a timezone that doesn't exist.
POST /api/settings/config wrote all of those with no checks at all, and the
webhook it stored is POSTed to on every sync - so a stored setting became an
outbound request to wherever it pointed. These check the write paths and the
send path that reads them back.
"""
import sys, types, os, tempfile, base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stub(n, a=()):
    m = types.ModuleType(n)
    for x in a: setattr(m, x, type(x, (), {}))
    sys.modules[n] = m; return m


for n in ("seleniumbase", "bs4", "halo"):
    try: __import__(n)
    except ImportError: stub(n, ("SB", "BeautifulSoup", "Halo"))
d = stub("dotenv"); d.load_dotenv = lambda *a, **k: None; d.set_key = lambda *a, **k: None


# Fernet stands in for the real thing: these suites do not install cryptography,
# and the settings paths only need encrypt/decrypt to round-trip.
class FakeFernet:
    def __init__(self, key): pass
    @staticmethod
    def generate_key(): return base64.urlsafe_b64encode(b"k" * 32)
    def encrypt(self, data): return base64.urlsafe_b64encode(data)
    def decrypt(self, token): return base64.urlsafe_b64decode(token)


c = types.ModuleType("cryptography"); f = types.ModuleType("cryptography.fernet")
f.Fernet = FakeFernet
f.InvalidToken = type("InvalidToken", (Exception,), {})
c.fernet = f
sys.modules["cryptography"] = c
sys.modules["cryptography.fernet"] = f

tmp = tempfile.mkdtemp()
import list_sync.utils.logger as lg
lg.DATA_DIR = tmp
import list_sync.encryption as encryption
from pathlib import Path
encryption.ENCRYPTION_KEY_FILE = Path(tmp) / ".encryption_key"
import list_sync.database as db
db.DB_FILE = os.path.join(tmp, "list_sync.db")
db.init_database()

import api_server
api_server.DB_FILE = db.DB_FILE

# Anything that escapes the guards shows up here rather than leaving the box.
outbound = []
import requests


def record_get(url, *a, **k):
    outbound.append(("GET", url))
    raise requests.exceptions.ConnectionError("no outbound requests in tests")


def record_post(url, *a, **k):
    outbound.append(("POST", url))
    raise requests.exceptions.ConnectionError("no outbound requests in tests")


requests.get = record_get
requests.post = record_post

from fastapi.testclient import TestClient
from list_sync.config import ConfigManager
client = TestClient(api_server.app)

fail = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)


def stored(key):
    return ConfigManager().get_setting(key)


print("=== POST /api/settings/config rejects what the wizard rejects ===")
for payload, name in [
    ({"discord_webhook": "http://169.254.169.254/latest/meta-data/"}, "webhook at metadata"),
    ({"discord_webhook": "http://127.0.0.1:5055/api/webhooks/1/x"}, "webhook at loopback"),
    ({"discord_webhook": "https://evil.example/api/webhooks/1/x"}, "webhook off Discord"),
    ({"discord_webhook": "https://discord.com.evil.example/api/webhooks/1/x"}, "suffix trick"),
    ({"discord_webhook": "https://discord.com/login"}, "Discord host, not a webhook"),
    ({"discord_webhook": "file:///etc/passwd"}, "file scheme"),
    ({"overseerr_url": "http://169.254.169.254"}, "seerr url at metadata"),
    ({"overseerr_url": "http://metadata.google.internal/"}, "seerr url at gcp metadata"),
    ({"overseerr_url": "gopher://127.0.0.1:11211/"}, "seerr url with gopher scheme"),
    ({"sync_interval": -5}, "negative sync interval"),
    ({"sync_interval": 0}, "zero sync interval"),
    ({"sync_interval": 100000}, "absurd sync interval"),
    ({"timezone": "Not/AZone"}, "nonexistent timezone"),
    ({"frontend_domain": "javascript:alert(1)"}, "javascript: frontend domain"),
]:
    r = client.post("/api/settings/config", json=payload)
    check(f"rejects {name}", r.status_code, 400)
    key = next(iter(payload))
    check(f"  and stores nothing for {name}", stored(key), None)

print()
print("=== legitimate settings still save ===")
r = client.post("/api/settings/config", json={
    "discord_webhook": "https://discord.com/api/webhooks/123/abcdef",
    "overseerr_url": "http://192.168.1.50:5055",
    "sync_interval": 24,
    "timezone": "Europe/London",
    "frontend_domain": "https://listsync.example.com",
    "imdb_lists": "ls123456789",
})
check("valid batch accepted", r.status_code, 200)
check("  webhook stored", stored("discord_webhook"), "https://discord.com/api/webhooks/123/abcdef")
check("  private seerr stored", stored("overseerr_url"), "http://192.168.1.50:5055")
check("  interval stored", str(stored("sync_interval")), "24")

print()
print("=== a masked placeholder is not mistaken for a bad value ===")
# The settings page reads secrets back masked and posts them straight back.
# save_setting skips those; validation must skip them too, or the round-trip
# fails on a mask that stands for a URL already known to be good.
masked = encryption.mask_sensitive_value("https://discord.com/api/webhooks/123/abcdef")
r = client.post("/api/settings/config", json={"discord_webhook": masked})
check("masked webhook accepted", r.status_code, 200)
check("  real webhook preserved", stored("discord_webhook"),
      "https://discord.com/api/webhooks/123/abcdef")

print()
print("=== setup wizard step 2: a disabled webhook is still checked ===")


class OKResponse:
    """Enough of a response for the Trakt client ID check to pass."""
    status_code = 200
    headers = {}
    def json(self): return {}
    def raise_for_status(self): pass


requests.get = lambda *a, **k: OKResponse()
r = client.post("/api/setup/step2/configuration", json={
    "trakt_client_id": "0123456789abcdef0123",
    "sync_interval": 24,
    "timezone": "UTC",
    "discord_webhook": "http://169.254.169.254/",
    "discord_enabled": False,   # used to skip the check while still saving
})
body = r.json()
check("step 2 refuses the webhook", body.get("valid"), False)
check("  and names the field", "discord_webhook" in body.get("errors", {}), True)
check("  and stores nothing new", stored("discord_webhook"),
      "https://discord.com/api/webhooks/123/abcdef")
requests.get = record_get

print()
print("=== the send path refuses a bad webhook however it got there ===")
from list_sync.notifications import discord as notifications
from list_sync.ui.display import SyncResults

for url, name in [
    ("http://169.254.169.254/", "metadata"),
    ("http://127.0.0.1:5055/api/webhooks/1/x", "loopback"),
    ("https://evil.example/api/webhooks/1/x", "off Discord"),
    ("https://discord.com/login", "Discord host, not a webhook"),
    ("file:///etc/passwd", "file scheme"),
]:
    outbound.clear()
    notifications.send_to_discord_webhook("test", SyncResults(), webhook_url=url)
    check(f"no request sent to {name}", outbound, [])

print()
print("=== encrypted-at-rest coverage ===")
for key in ("overseerr_api_key", "seerr_api_key", "trakt_client_id", "discord_webhook",
            "tmdb_key", "tvdb_key", "simkl_client_id", "simkl_user_token"):
    check(f"{key} is encrypted at rest", encryption.should_encrypt(key), True)
check("a list ID is not treated as a secret", encryption.should_encrypt("imdb_lists"), False)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
