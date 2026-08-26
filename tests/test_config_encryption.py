"""
config.enc gets a salt and a key derivation function, and still opens old files.

The password used to be padded to 32 bytes and used as the Fernet key directly:
no salt, no work factor, so a stolen config.enc could be brute-forced as fast as
the attacker could try passwords, and two installs with the same password had
the same key. New files derive the key with PBKDF2 over a random salt. Files
written before that must keep opening, or an upgrade locks people out of their
own credentials.

cryptography is not installed for these suites, so Fernet and PBKDF2 are stood
in for. What is being checked is the file format and which derivation each path
picks - not the primitives themselves.
"""
import sys, types, os, tempfile, base64, hashlib, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stub(n, a=()):
    m = types.ModuleType(n)
    for x in a: setattr(m, x, type(x, (), {}))
    sys.modules[n] = m; return m


for n in ("seleniumbase", "bs4", "halo"):
    try: __import__(n)
    except ImportError: stub(n, ("SB", "BeautifulSoup", "Halo"))
d = stub("dotenv"); d.load_dotenv = lambda *a, **k: None; d.set_key = lambda *a, **k: None


class FakeFernet:
    """Keyed but not secret: the key is carried in the token so it can be read back."""

    def __init__(self, key):
        self.key = key

    def encrypt(self, data):
        return base64.urlsafe_b64encode(self.key + b"::" + data)

    def decrypt(self, token):
        key, _, data = base64.urlsafe_b64decode(token).partition(b"::")
        if key != self.key:
            raise ValueError("wrong key")
        return data


class FakePBKDF2HMAC:
    def __init__(self, algorithm=None, length=32, salt=b"", iterations=1):
        self.length, self.salt, self.iterations = length, salt, iterations

    def derive(self, password):
        # Same shape as the real thing: password and salt both feed the result.
        return hashlib.pbkdf2_hmac("sha256", password, self.salt, 1, self.length)


c = types.ModuleType("cryptography")
f = types.ModuleType("cryptography.fernet")
f.Fernet = FakeFernet
f.InvalidToken = type("InvalidToken", (Exception,), {})
hazmat = types.ModuleType("cryptography.hazmat")
primitives = types.ModuleType("cryptography.hazmat.primitives")
primitives.hashes = types.ModuleType("cryptography.hazmat.primitives.hashes")
primitives.hashes.SHA256 = lambda: "sha256"
kdf = types.ModuleType("cryptography.hazmat.primitives.kdf")
pbkdf2 = types.ModuleType("cryptography.hazmat.primitives.kdf.pbkdf2")
pbkdf2.PBKDF2HMAC = FakePBKDF2HMAC
c.fernet, c.hazmat = f, hazmat
for name, mod in [
    ("cryptography", c),
    ("cryptography.fernet", f),
    ("cryptography.hazmat", hazmat),
    ("cryptography.hazmat.primitives", primitives),
    ("cryptography.hazmat.primitives.hashes", primitives.hashes),
    ("cryptography.hazmat.primitives.kdf", kdf),
    ("cryptography.hazmat.primitives.kdf.pbkdf2", pbkdf2),
]:
    sys.modules[name] = mod

tmp = tempfile.mkdtemp()
import list_sync.utils.logger as lg
lg.DATA_DIR = tmp

from list_sync import config

fail = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)


DATA = {"overseerr_url": "http://seerr:5055", "api_key": "s3cret", "requester_user_id": "1"}

print("=== new files carry a salt ===")
blob = config.encrypt_config(DATA, "hunter2")
check("marked with the format magic", blob.startswith(config._CONFIG_MAGIC), True)
check("round-trips", config.decrypt_config(blob, "hunter2"), DATA)

again = config.encrypt_config(DATA, "hunter2")
salt_of = lambda b: b[len(config._CONFIG_MAGIC):len(config._CONFIG_MAGIC) + config._CONFIG_SALT_BYTES]
check("same password, different salt", salt_of(blob) == salt_of(again), False)
check("so the same password gives different ciphertext", blob == again, False)
check("and the second file round-trips too", config.decrypt_config(again, "hunter2"), DATA)

print()
print("=== the key actually depends on the password and the salt ===")
key_a = config._derive_key("hunter2", b"\x00" * 16)
check("same password and salt is stable", config._derive_key("hunter2", b"\x00" * 16), key_a)
check("a different password changes it", config._derive_key("other", b"\x00" * 16) == key_a, False)
check("a different salt changes it", config._derive_key("hunter2", b"\x01" * 16) == key_a, False)
check("the password is not the key", config._legacy_key("hunter2") == key_a, False)

print()
print("=== a file from before the change still opens ===")
legacy = FakeFernet(config._legacy_key("hunter2")).encrypt(json.dumps(DATA).encode())
check("no magic on a legacy file", legacy.startswith(config._CONFIG_MAGIC), False)
check("legacy file round-trips", config.decrypt_config(legacy, "hunter2"), DATA)

print()
print("=== a wrong password still fails ===")
for blob_name, blob_bytes in [("new format", blob), ("legacy format", legacy)]:
    try:
        config.decrypt_config(blob_bytes, "wrong")
        raised = False
    except Exception:
        raised = True
    check(f"wrong password rejected ({blob_name})", raised, True)

print()
print("=== the work factor is set, not left at a token value ===")
check("PBKDF2 rounds at or above the OWASP floor",
      config._CONFIG_KDF_ROUNDS >= 600_000, True)
check("salt is at least 16 bytes", config._CONFIG_SALT_BYTES >= 16, True)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
