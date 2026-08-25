"""Exercise in-progress sync tracking against a real sqlite db.

The bug these cover: a sync that never reaches end_sync_in_db leaves
in_progress = 1 behind, and every dashboard then reports "Sync in Progress"
forever. Liveness has to come from the heartbeat, because a full sync records
the PID of the long-lived core process, which outlives the sync it runs.
"""
import datetime
import os, sys, tempfile, types

tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# helpers.py imports seleniumbase at module scope and api_server pulls in
# cryptography; none of the code under test touches a browser or a key, so stub
# them out rather than installing Chrome and building native extensions.
def stub(name, attrs=()):
    mod = types.ModuleType(name)
    for attr in attrs:
        setattr(mod, attr, type(attr, (), {}))
    sys.modules[name] = mod
    return mod

for name in ("seleniumbase", "bs4", "dotenv", "halo"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            mod = types.ModuleType(name)
            for attr in ("SB", "BeautifulSoup", "load_dotenv", "set_key", "find_dotenv", "Halo"):
                setattr(mod, attr, lambda *a, **k: None)
            sys.modules[name] = mod

crypto = stub("cryptography")
crypto.fernet = stub("cryptography.fernet", ("Fernet", "InvalidToken"))

import sqlite3

import list_sync.database as db
db.DB_FILE = os.path.join(tmp, "list_sync.db")
db.init_database()

from list_sync.database import (
    start_sync_in_db, end_sync_in_db, cancel_sync_in_db, heartbeat_sync_in_db,
    get_current_sync_status, clear_stale_syncs,
)
from list_sync.utils.sync_status import (
    get_sync_staleness_reason, parse_db_timestamp, start_sync_heartbeat,
)

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        fail.append(label)


def set_activity(session_id, minutes_ago):
    """Backdate a record's heartbeat, as if the process had gone quiet."""
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db.DB_FILE) as conn:
        conn.execute(
            "UPDATE sync_history SET start_time = ?, last_heartbeat = ? WHERE session_id = ?",
            (stamp, stamp, session_id),
        )


# --- timestamps are stored as UTC and must be read back as UTC ---
now = datetime.datetime.now(datetime.timezone.utc)
sqlite_now = now.strftime("%Y-%m-%d %H:%M:%S")
parsed = parse_db_timestamp(sqlite_now)
check("sqlite timestamp parsed as utc", parsed is not None and parsed.tzinfo is not None, True)
check("utc timestamp reads as now", abs((now - parsed).total_seconds()) < 2, True)
check("iso timestamp parsed", parse_db_timestamp("2026-01-02T03:04:05Z").hour, 3)
check("missing timestamp is none", parse_db_timestamp(None), None)

# --- a sync that finishes normally clears itself ---
start_sync_in_db(session_id="finished", sync_type="full")
check("running sync is reported", get_current_sync_status()["session_id"], "finished")
end_sync_in_db(session_id="finished", status="completed")
check("finished sync is not reported", get_current_sync_status(), None)

# --- a cancelled sync must clear in_progress, not just set the status ---
start_sync_in_db(session_id="cancelled", sync_type="full")
check("cancel updates record", cancel_sync_in_db("cancelled"), True)
check("cancelled sync is not reported", get_current_sync_status(), None)
with sqlite3.connect(db.DB_FILE) as conn:
    row = conn.execute(
        "SELECT in_progress, status FROM sync_history WHERE session_id = 'cancelled'"
    ).fetchone()
check("cancelled record is closed", row, (0, "cancelled"))

# --- a live PID is not evidence of a live sync ---
# This is the stuck-status bug: full syncs record the core process PID, which
# stays alive between syncs, so PID liveness alone never clears the record.
abandoned = {
    "session_id": "abandoned",
    "pid": os.getpid(),  # alive, but not running a sync
    "start_time": (now - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
    "last_heartbeat": (now - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
}
check("silent record is stale despite live pid",
      bool(get_sync_staleness_reason(abandoned)), True)

# --- a sync that is still heartbeating is left alone ---
alive = dict(abandoned, last_heartbeat=sqlite_now)
check("heartbeating record is not stale", get_sync_staleness_reason(alive), None)

# --- a dead PID is spotted without waiting for the timeout ---
dead_pid = {
    "session_id": "dead",
    "pid": 2 ** 22,  # above every pid_max, so it cannot exist
    "start_time": (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
    "last_heartbeat": (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
}
check("dead pid is stale before timeout", bool(get_sync_staleness_reason(dead_pid)), True)

# --- a record that has only just been written is never stale ---
fresh = {"session_id": "fresh", "pid": 2 ** 22, "start_time": sqlite_now, "last_heartbeat": sqlite_now}
check("brand new record is not stale", get_sync_staleness_reason(fresh), None)

# --- an abandoned record is cleared out of the live status ---
start_sync_in_db(session_id="stuck", sync_type="full")
set_activity("stuck", minutes_ago=60)
check("stuck sync is cleared", get_current_sync_status(), None)
with sqlite3.connect(db.DB_FILE) as conn:
    row = conn.execute(
        "SELECT in_progress, status FROM sync_history WHERE session_id = 'stuck'"
    ).fetchone()
check("stuck record is closed as interrupted", row, (0, "interrupted"))

# --- a heartbeat keeps an otherwise stale record alive ---
start_sync_in_db(session_id="slow", sync_type="full")
set_activity("slow", minutes_ago=60)
check("heartbeat updates record", heartbeat_sync_in_db("slow"), True)
check("heartbeating sync survives cleanup", get_current_sync_status()["session_id"], "slow")
end_sync_in_db(session_id="slow", status="completed")

# --- starting a sync clears whatever the last one left behind ---
start_sync_in_db(session_id="leaked", sync_type="full")
set_activity("leaked", minutes_ago=60)
start_sync_in_db(session_id="current", sync_type="full")
check("new sync is the reported one", get_current_sync_status()["session_id"], "current")
with sqlite3.connect(db.DB_FILE) as conn:
    open_records = conn.execute("SELECT COUNT(*) FROM sync_history WHERE in_progress = 1").fetchone()[0]
check("only one open record remains", open_records, 1)
end_sync_in_db(session_id="current", status="completed")

# --- the heartbeat thread writes while the sync runs, and stops after ---
start_sync_in_db(session_id="threaded", sync_type="single", list_type="imdb", list_id="ls1")
set_activity("threaded", minutes_ago=60)
beat = start_sync_heartbeat("threaded", interval_seconds=0.2)
import time
time.sleep(0.6)
check("heartbeat thread refreshes record", get_current_sync_status()["session_id"], "threaded")
beat.stop()
check("heartbeat thread stops", beat._thread, None)
end_sync_in_db(session_id="threaded", status="completed")

# --- cleanup reports what it closed ---
start_sync_in_db(session_id="reported", sync_type="full")
set_activity("reported", minutes_ago=60)
cleared = clear_stale_syncs()
check("cleared records are reported", [c["session_id"] for c in cleared], ["reported"])
check("clearing is idempotent", clear_stale_syncs(), [])

# --- upgrading a database written before last_heartbeat existed ---
legacy_db = os.path.join(tmp, "legacy.db")
with sqlite3.connect(legacy_db) as conn:
    conn.execute('''
        CREATE TABLE sync_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            sync_type TEXT NOT NULL,
            in_progress INTEGER DEFAULT 1,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            status TEXT,
            list_type TEXT,
            list_id TEXT,
            pid INTEGER,
            total_items INTEGER DEFAULT 0,
            items_requested INTEGER DEFAULT 0,
            items_skipped INTEGER DEFAULT 0,
            items_errors INTEGER DEFAULT 0,
            error_message TEXT
        )
    ''')
    # A full sync from an hour ago, holding the PID of the core process that
    # ran it - still alive, because that process runs for the life of the
    # container. This is the record that left the dashboard stuck.
    stale_start = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO sync_history (session_id, sync_type, in_progress, start_time, pid, status)"
        " VALUES ('legacy_stuck', 'full', 1, ?, ?, 'running')",
        (stale_start, os.getpid()),
    )

db.DB_FILE = legacy_db
db.init_database()
legacy_columns = [r[1] for r in sqlite3.connect(legacy_db).execute("PRAGMA table_info(sync_history)")]
check("upgrade adds last_heartbeat", "last_heartbeat" in legacy_columns, True)
check("upgrade is repeatable", (db.init_database(), "last_heartbeat" in legacy_columns)[1], True)

# --- the live endpoint reports the stuck sync as idle and closes the record ---
import api_server
api_server.DB_FILE = legacy_db

from fastapi.testclient import TestClient
client = TestClient(api_server.app)

body = client.get("/api/sync/status/live").json()
check("stuck sync reported idle", body.get("is_running"), False)
check("stuck sync status idle", body.get("status"), "idle")
with sqlite3.connect(legacy_db) as conn:
    row = conn.execute(
        "SELECT in_progress, status FROM sync_history WHERE session_id = 'legacy_stuck'"
    ).fetchone()
check("stuck record closed by endpoint", row, (0, "interrupted"))

# --- a genuinely running sync is still reported as running ---
start_sync_in_db(session_id="really_running", sync_type="full")
body = client.get("/api/sync/status/live").json()
check("running sync reported running", body.get("is_running"), True)
check("running sync status", body.get("status"), "running_full")
check("running sync session", body.get("session_id"), "really_running")
# Duration is computed against UTC, so a sync that just started reads as ~0
# seconds rather than as the local UTC offset.
duration = body.get("duration_seconds")
check("duration is near zero", duration is not None and abs(duration) < 60, True)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
