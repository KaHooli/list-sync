"""Adversarial tests for the outbound-URL validator."""
import sys, types, socket

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def stub(n, a=()):
    m = types.ModuleType(n)
    for x in a: setattr(m, x, type(x, (), {}))
    sys.modules[n] = m; return m
for n in ("seleniumbase", "bs4", "halo"):
    try: __import__(n)
    except ImportError: stub(n, ("SB", "BeautifulSoup", "Halo"))

import logging
logging.disable(logging.CRITICAL)

from list_sync.utils import url_safety as us
from list_sync.utils.url_safety import (
    validate_outbound_url, assert_safe_url, DISCORD_WEBHOOK_HOSTS,
)

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)

# Deterministic DNS: names map to fixed addresses, unknown names fail.
DNS = {
    "example.com":        ["93.184.216.34"],
    "image.tmdb.org":     ["104.16.1.1"],
    "discord.com":        ["162.159.128.233"],
    "ptb.discord.com":    ["162.159.128.233"],
    "notdiscord.com":     ["93.184.216.34"],
    "discord.com.evil.example": ["93.184.216.34"],
    "evil.example":       ["127.0.0.1"],            # rebinding-style answer
    "split.example":      ["93.184.216.34", "10.0.0.5"],  # one bad among good
    "meta.example":       ["169.254.169.254"],
    "v6loop.example":     ["::1"],
    "mapped.example":     ["::ffff:127.0.0.1"],
    "seerr":              ["172.18.0.5"],
    "ecs.example":        ["169.254.170.2"],
}
def fake_getaddrinfo(host, *a, **k):
    if host in DNS:
        return [(None, None, None, "", (ip, 0)) for ip in DNS[host]]
    raise socket.gaierror(-2, "Name or service not known")
us.socket.getaddrinfo = fake_getaddrinfo

def allowed(url, **kw):
    return validate_outbound_url(url, **kw)[0]

# --- public hosts pass ---
check("public host", allowed("https://example.com/x.jpg"), True)
check("public cdn", allowed("https://image.tmdb.org/t/p/w500/a.jpg"), True)
check("http is fine", allowed("http://example.com/"), True)

# --- schemes ---
for bad in ("file:///etc/passwd", "gopher://example.com/", "ftp://example.com/",
            "dict://example.com:11211/", "jar:http://example.com!/"):
    check(f"scheme blocked: {bad.split(':')[0]}", allowed(bad), False)

# --- private / loopback / link-local / reserved ---
for bad in ("http://127.0.0.1:8080/", "http://localhost:5055/",
            "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/", "http://[::1]/", "http://[fe80::1]/"):
    check(f"blocked: {bad}", allowed(bad), False)

# --- literal metadata blocked even when private is allowed ---
check("metadata blocked despite allow_private",
      allowed("http://169.254.169.254/latest/meta-data/", allow_private=True), False)
check("ecs metadata blocked despite allow_private",
      allowed("http://169.254.170.2/v2/credentials", allow_private=True), False)
check("gcp metadata hostname blocked",
      allowed("http://metadata.google.internal/computeMetadata/v1/", allow_private=True), False)

# --- private allowed only when asked ---
check("private refused by default", allowed("http://seerr:5055/api/v1/status"), False)
check("private allowed when asked",
      allowed("http://seerr:5055/api/v1/status", allow_private=True), True)
check("real user's seerr url works",
      allowed("http://seerr:5055", allow_private=True), True)

# --- DNS-based evasion ---
check("name resolving to loopback", allowed("http://evil.example/"), False)
check("name resolving to metadata", allowed("http://meta.example/"), False)
check("one private answer among several", allowed("http://split.example/"), False)
check("ipv6 loopback by name", allowed("http://v6loop.example/"), False)
check("ipv4-mapped ipv6 loopback", allowed("http://mapped.example/"), False)
check("metadata by name, private allowed",
      allowed("http://ecs.example/", allow_private=True), False)

# --- credentials and malformed ---
check("embedded credentials", allowed("https://user:pass@example.com/"), False)
check("credentials disguising host",
      allowed("https://discord.com@evil.example/"), False)
check("no host", allowed("https:///path"), False)
check("empty", allowed(""), False)
check("none", allowed(None), False)
check("not a url", allowed("just some text"), False)
check("unresolvable", allowed("https://nx.invalid/"), False)

# --- host allowlist (Discord) ---
D = dict(allowed_hosts=DISCORD_WEBHOOK_HOSTS)
check("real discord webhook",
      allowed("https://discord.com/api/webhooks/123/abc", **D), True)
check("discord subdomain",
      allowed("https://ptb.discord.com/api/webhooks/1/x", **D), True)
check("attacker host refused", allowed("http://attacker-controlled-domain/", **D), False)
check("lookalike suffix refused", allowed("https://notdiscord.com/api", **D), False)
check("suffix-append trick refused", allowed("https://discord.com.evil.example/", **D), False)
check("host in path refused", allowed("https://evil.example/discord.com", **D), False)
check("metadata with allowlist refused",
      allowed("http://169.254.169.254/", **D), False)

# --- trailing dot and case normalisation must not bypass the allowlist ---
check("trailing dot normalised", allowed("https://discord.com./api/webhooks/1/x", **D), True)
check("uppercase host normalised", allowed("https://DISCORD.COM/api/webhooks/1/x", **D), True)
check("uppercase evil still refused", allowed("https://EVIL.EXAMPLE/", **D), False)

# --- the PoC from the upstream issue ---
check("issue #79 PoC blocked",
      allowed("http://attacker-controlled-domain/", allowed_hosts=DISCORD_WEBHOOK_HOSTS), False)

# --- assert_safe_url raises with a reason ---
try:
    assert_safe_url("http://127.0.0.1/", what="Image URL")
    check("assert raises", "returned", "ValueError")
except ValueError as e:
    check("assert raises", "ValueError" in type(e).__name__, True)
    check("reason mentions the target", "127.0.0.1" in str(e), True)
check("assert returns url when ok",
      assert_safe_url("https://example.com/a.jpg"), "https://example.com/a.jpg")
check("assert strips whitespace",
      assert_safe_url("  https://example.com/a.jpg  "), "https://example.com/a.jpg")

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
