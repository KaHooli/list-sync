# Tests

Behavioural tests for the sync path, the Seerr client, the API server and
the URL validator.

```bash
pip install -r tests/requirements.txt
python tests/run_all.py            # everything
python tests/run_all.py imdb       # suites matching a substring
python tests/test_url_safety.py    # one suite, full output
```

## How these are written

Each suite is a standalone script that prints one line per check and exits
non-zero if any failed. There is no pytest dependency, and `run_all.py` runs
each suite in its own process so one suite's stubs cannot leak into another.

They stub the heavy dependencies — `seleniumbase`, `cryptography`,
`python-dotenv`, `bs4`, `halo` — rather than installing them. Nothing here
drives a browser or reaches the network, so the whole run takes a couple of
seconds and needs no Chrome, no API keys, and no Seerr.

Where a test needs something real it gets a real one: a real SQLite database
in a temporary directory, FastAPI's `TestClient` against the actual app, a
fake SeleniumBase that records navigations, and a deterministic fake resolver
for DNS.

## Suites

| Suite | Covers |
|---|---|
| `test_db` | list-ID normalisation, user and `last_synced` surviving re-save, legacy duplicate rows |
| `test_overseerr` | `X-Api-User`, response classification across 201/400/401/403/409/500, requester validation |
| `test_api` | add, reassign and reject flows through the HTTP layer |
| `test_multiuser` | requester fan-out and dedup, 4K vs non-4K, malformed request entries |
| `test_env_lists` | per-list user syntax, `OVERSEERR_USER_ID` fallback, Trakt colon syntax |
| `test_imdb_json` | schema-agnostic extraction, AWS WAF detection and backoff |
| `test_imdb_pagination` | the watchlist runaway, scroll termination, repeated pages |
| `test_tmdb_resolve` | exact `tt` resolution and every failure path returning `None` |
| `test_startup` | recovery from a slow Seerr, interactivity detection, clean exit |
| `test_url_safety` | SSRF validator: schemes, private ranges, metadata, DNS evasion, allowlists |
| `test_ssrf_endpoints` | the guards rejecting at the HTTP layer, with a sentinel asserting nothing escapes |
| `test_settings_validation` | settings writes rejecting what the wizard rejects, masked values round-tripping, the Discord sink |
| `test_config_encryption` | `config.enc` salt and key derivation, and files from before it still opening |
| `test_image_sniff` | image type detection, cross-checked against `imghdr` where it still exists |

## Adding one

Name it `test_*.py` in this directory and `run_all.py` will pick it up. Follow
the existing shape: a `check(label, got, want)` that appends to a `fail` list,
and `sys.exit(1 if fail else 0)` at the end. Print `PASS` at the start of a
passing line — the runner counts those.

## Python versions

CI runs 3.12 and 3.13. 3.12 is the floor in `pyproject.toml` and what the
Dockerfile ships; 3.13 is there to catch standard-library removals, which is
how the `imghdr` breakage in the image proxy surfaced.

Those two jobs report as `Python 3.12` and `Python 3.13`, so their names move
whenever the matrix does. Branch protection should require the `Tests` job
instead: it passes only when every leg of the matrix passed, and its name stays
put across version bumps.
