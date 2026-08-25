#!/usr/bin/env python3
"""
Diagnose why a list's requests aren't being attributed to the intended Seerr user.

Cross-references the lists in ListSync's database against the users on the
Seerr server, and reports the specific reasons a list would
fail to request as its assigned user.

Run it inside the ListSync container, where the database and the Seerr
credentials are already present:

    docker exec -it listsync-full python3 development-files/scripts/diagnose_list_users.py

Standard library only, and read-only: it never writes to the database and
never submits a request. The API key is read from the environment and is
never printed.
"""

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

DB_PATH = os.getenv("LISTSYNC_DB", "./data/list_sync.db")

# Seerr permission bits, unchanged from Overseerr (server/lib/permissions.ts).
PERMISSION_ADMIN = 2
PERMISSION_MANAGE_REQUESTS = 16
PERMISSION_REQUEST = 32
PERMISSION_REQUEST_4K = 1024
PERMISSION_REQUEST_MOVIE = 262144
PERMISSION_REQUEST_TV = 524288


def api_get(base_url, api_key, path):
    """GET a JSON document from the Seerr API."""
    url = f"{base_url.rstrip('/')}{path}"
    request = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from {path}"
    except Exception as e:
        return None, f"{type(e).__name__} contacting {path}: {e}"


def load_lists(db_path):
    """Read the configured lists, tolerating databases without a user_id column."""
    if not os.path.exists(db_path):
        return None, f"Database not found at {db_path} (set LISTSYNC_DB to override)"

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(lists)")
        columns = {row[1] for row in cursor.fetchall()}
        if not columns:
            return None, "No 'lists' table in the database"

        has_user = "user_id" in columns
        select = "SELECT list_type, list_id, user_id FROM lists" if has_user \
            else "SELECT list_type, list_id, NULL FROM lists"
        cursor.execute(select)
        rows = cursor.fetchall()

    return {"rows": rows, "has_user_column": has_user}, None


def describe_permissions(bits):
    """Turn a permission bitfield into the request-related names it contains."""
    if not isinstance(bits, int):
        return []
    names = []
    for bit, name in (
        (PERMISSION_ADMIN, "ADMIN"),
        (PERMISSION_MANAGE_REQUESTS, "MANAGE_REQUESTS"),
        (PERMISSION_REQUEST, "REQUEST"),
        (PERMISSION_REQUEST_MOVIE, "REQUEST_MOVIE"),
        (PERMISSION_REQUEST_TV, "REQUEST_TV"),
        (PERMISSION_REQUEST_4K, "REQUEST_4K"),
    ):
        if bits & bit:
            names.append(name)
    return names


def can_request(bits):
    """Whether a permission bitfield allows making requests at all."""
    if not isinstance(bits, int):
        return True  # unknown shape - don't cry wolf
    if bits & PERMISSION_ADMIN:
        return True
    return bool(bits & (PERMISSION_REQUEST | PERMISSION_REQUEST_MOVIE | PERMISSION_REQUEST_TV))


def looks_like_url(value):
    return str(value).startswith(("http://", "https://"))


def seerr_env(name, legacy_name, default=""):
    """
    Read a SEERR_* setting, accepting the old OVERSEERR_* name.

    Duplicated from list_sync.config rather than imported: this script is
    standard library only so it runs under docker exec with no install step.
    An empty value counts as unset, because compose defines the variable as
    an empty string when the user has not set it.
    """
    for candidate in (name, legacy_name):
        value = os.getenv(candidate, "").strip()
        if value:
            return value
    return default


def main():
    seerr_url = seerr_env("SEERR_URL", "OVERSEERR_URL")
    api_key = seerr_env("SEERR_API_KEY", "OVERSEERR_API_KEY")
    default_user = seerr_env("SEERR_USER_ID", "OVERSEERR_USER_ID", "1")
    is_4k = seerr_env("SEERR_4K", "OVERSEERR_4K", "false").lower() == "true"

    print("=" * 72)
    print("ListSync -> Seerr requester diagnostic")
    print("=" * 72)

    if not seerr_url or not api_key:
        print("\nSEERR_URL and SEERR_API_KEY must be set in the environment.")
        print("Run this inside the ListSync container so it picks them up automatically.")
        return 2

    print(f"\nSeerr:            {seerr_url}")
    print(f"Database:         {DB_PATH}")
    print(f"Default user:     SEERR_USER_ID={default_user}")
    print(f"Requesting 4K:    {is_4k}")

    # --- lists -------------------------------------------------------------
    lists_data, error = load_lists(DB_PATH)
    if error:
        print(f"\n{error}")
        return 2

    rows = lists_data["rows"]
    if not lists_data["has_user_column"]:
        print("\n! This database has no user_id column on 'lists', so every list")
        print("  requests as the admin account. Upgrade ListSync to get per-list users.")

    # --- users -------------------------------------------------------------
    users_doc, error = api_get(seerr_url, api_key, "/api/v1/user?take=200")
    if error:
        print(f"\nCould not list Seerr users: {error}")
        print("Check SEERR_URL and that the API key is valid.")
        return 2

    users = {str(u.get("id")): u for u in users_doc.get("results", [])}
    print(f"\nSeerr users found: {len(users)}")
    for user_id, user in sorted(users.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        name = user.get("displayName") or user.get("username") or "?"
        perms = describe_permissions(user.get("permissions"))
        flag = "" if can_request(user.get("permissions")) else "   <-- CANNOT REQUEST"
        print(f"  {user_id:>4}  {name:<28} {','.join(perms) or 'none'}{flag}")

    # --- quotas ------------------------------------------------------------
    assigned_users = {str(r[2] or default_user) for r in rows} or {default_user}
    quotas = {}
    for user_id in sorted(assigned_users):
        quota, error = api_get(seerr_url, api_key, f"/api/v1/user/{user_id}/quota")
        if not error and quota:
            quotas[user_id] = quota

    if quotas:
        print("\nRequest quotas for the users your lists use:")
        for user_id, quota in sorted(quotas.items()):
            for kind in ("movie", "tv"):
                q = quota.get(kind) or {}
                if q.get("limit"):
                    state = "RESTRICTED - requests will be rejected" if q.get("restricted") else "ok"
                    print(f"  user {user_id} {kind:<6} {q.get('used', '?')}/{q.get('limit')} "
                          f"per {q.get('days', '?')}d  {state}")

    # --- the actual cross-reference ----------------------------------------
    print("\n" + "-" * 72)
    print("Lists")
    print("-" * 72)

    problems = []
    for list_type, list_id, user_id in sorted(rows):
        effective = str(user_id or default_user)
        user = users.get(effective)
        name = (user.get("displayName") or user.get("username")) if user else None

        notes = []
        if not user_id:
            notes.append(f"no user assigned, falls back to {default_user}")
        if not user:
            notes.append(f"USER {effective} DOES NOT EXIST on this Seerr")
            problems.append(f"{list_type}:{list_id} -> user {effective} does not exist")
        elif not can_request(user.get("permissions")):
            notes.append("user lacks the Request permission")
            problems.append(f"{list_type}:{list_id} -> '{name}' cannot make requests")

        quota = quotas.get(effective) or {}
        for kind in ("movie", "tv"):
            if (quota.get(kind) or {}).get("restricted"):
                notes.append(f"{kind} quota exhausted")
                problems.append(f"{list_type}:{list_id} -> '{name}' has hit their {kind} quota")

        if list_type == "imdb" and looks_like_url(list_id):
            notes.append("stored as a URL (see note below)")

        if is_4k and user and isinstance(user.get("permissions"), int):
            perms = user["permissions"]
            if not (perms & PERMISSION_ADMIN) and not (perms & PERMISSION_REQUEST_4K):
                notes.append("4K requested but user lacks REQUEST_4K")
                problems.append(f"{list_type}:{list_id} -> '{name}' cannot make 4K requests")

        label = name or "unknown"
        print(f"\n  {list_type}:{list_id}")
        print(f"    requests as: {effective} ({label})")
        for note in notes:
            print(f"    - {note}")

    # --- summary -----------------------------------------------------------
    print("\n" + "=" * 72)
    if problems:
        print(f"{len(problems)} problem(s) found:\n")
        for problem in problems:
            print(f"  * {problem}")
    else:
        print("No user/permission/quota problems found in the configuration.")

    url_stored = [r for r in rows if r[0] == "imdb" and looks_like_url(r[1])]
    if url_stored:
        print(f"\nNote: {len(url_stored)} IMDb list(s) are stored as full URLs. On ListSync")
        print("0.6.6 the single-list sync matches the stored ID with an exact string")
        print("compare, so syncing one of these by its bare 'ls...' ID misses the lookup")
        print("and silently falls back to user 1. Look for this line in the log:")
        print('  "List not found in database, using default user_id: 1"')

    print("=" * 72)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
