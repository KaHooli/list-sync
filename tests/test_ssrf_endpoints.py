"""End-to-end: the SSRF guards must actually reject at the HTTP layer."""
import sys, types, os, tempfile, socket

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

tmp = tempfile.mkdtemp()
import list_sync.utils.logger as lg
lg.DATA_DIR = tmp
import list_sync.database as db
db.DB_FILE = os.path.join(tmp, "list_sync.db")
db.init_database()

import api_server
api_server.DB_FILE = db.DB_FILE

# Any real outbound request during this test is itself a failure.
import requests
def forbidden(*a, **k):
    raise AssertionError(f"OUTBOUND REQUEST ESCAPED THE GUARD: {a} {k}")
requests.get = forbidden
requests.post = forbidden

from fastapi.testclient import TestClient
client = TestClient(api_server.app)

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

print("=== image proxy: the read-SSRF primitive ===")
for target, name in [
    ("http://169.254.169.254/latest/meta-data/iam/security-credentials/", "aws metadata"),
    ("http://127.0.0.1:5055/api/v1/status", "loopback"),
    ("http://192.168.1.1/", "lan host"),
    ("http://10.0.0.5:8080/admin", "private range"),
    ("http://[::1]/", "ipv6 loopback"),
    ("file:///etc/passwd", "file scheme"),
    ("http://metadata.google.internal/computeMetadata/v1/", "gcp metadata"),
]:
    r = client.get("/api/images/proxy", params={"url": target})
    check(f"proxy blocks {name}", r.status_code, 400)

print()
print("=== discord webhook test: issue #79 ===")
for body, name, want in [
    ({"webhook_url": "http://attacker-controlled-domain/"}, "issue #79 PoC", 400),
    ({"webhook_url": "http://169.254.169.254/"}, "metadata", 400),
    ({"webhook_url": "http://127.0.0.1:5055/"}, "loopback", 400),
    ({"webhook_url": "https://notdiscord.com/api/webhooks/1/x"}, "lookalike host", 400),
    ({"webhook_url": "https://discord.com.evil.example/x"}, "suffix trick", 400),
    ({"webhook_url": "file:///etc/passwd"}, "file scheme", 400),
]:
    r = client.post("/api/notifications/test", json=body)
    check(f"discord blocks {name}", r.status_code, want)

print()
print("=== overseerr connection test ===")
for target, name in [
    ("http://169.254.169.254", "metadata"),
    ("file:///etc/passwd", "file scheme"),
    ("gopher://127.0.0.1:11211/", "gopher"),
]:
    r = client.post("/api/overseerr/test", json={
        "overseerr_url": target, "overseerr_api_key": "k", "overseerr_user_id": "1"
    })
    # endpoint answers 200 with valid:false rather than an HTTP error
    body = r.json() if r.status_code == 200 else {}
    blocked = r.status_code >= 400 or body.get("valid") is False
    check(f"overseerr test blocks {name}", blocked, True)

print()
print("=== a private Seerr must still be permitted ===")
# 'seerr' resolves on the user's Docker network, not in this container, so
# stand in a resolver that answers the way theirs does.
from list_sync.utils import url_safety as us
from list_sync.utils.url_safety import validate_outbound_url
us.socket.getaddrinfo = lambda host, *a, **k: (
    [(None, None, None, "", ("172.18.0.5", 0))] if host == "seerr"
    else (_ for _ in ()).throw(socket.gaierror(-2, "Name or service not known"))
)
ok, reason = validate_outbound_url("http://seerr:5055", allow_private=True)
check("seerr:5055 still allowed", ok, True)
print(f"      reason: {reason}")
check("but seerr blocked when private is not permitted",
      validate_outbound_url("http://seerr:5055", allow_private=False)[0], False)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
