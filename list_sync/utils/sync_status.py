"""
In-memory sync status tracking for real-time sync state monitoring.
"""

import logging
import threading
import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
import json
import os


# A running sync bumps its database row every few seconds (see
# start_sync_heartbeat). A row that has not been touched for this long belongs
# to a sync that died without cleaning up - a crash, a kill, or a container
# restart - and must not keep the UI reporting "Sync in Progress" forever.
DEFAULT_SYNC_STALE_SECONDS = 15 * 60

# How often a running sync writes its heartbeat.
SYNC_HEARTBEAT_INTERVAL_SECONDS = 30

# A record is only judged on a dead PID once it is this old, so a sync that has
# just written its row is never mistaken for an abandoned one.
_MIN_AGE_BEFORE_PID_CHECK_SECONDS = 60


@dataclass
class SyncState:
    """Current sync state information"""
    is_running: bool = False
    sync_type: Optional[str] = None  # 'full' or 'single'
    session_id: Optional[str] = None
    start_time: Optional[datetime.datetime] = None
    list_type: Optional[str] = None  # For single list syncs
    list_id: Optional[str] = None  # For single list syncs
    pid: Optional[int] = None  # Main process PID
    sync_subprocess_pid: Optional[int] = None  # PID of subprocess running sync (for immediate termination)
    cancellation_requested: bool = False


class SyncStatusTracker:
    """Thread-safe singleton for tracking sync status"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(SyncStatusTracker, cls).__new__(cls)
                    cls._instance._state = SyncState()
                    cls._instance._state_lock = threading.Lock()
        return cls._instance
    
    def start_sync(
        self,
        sync_type: str,
        session_id: str,
        list_type: Optional[str] = None,
        list_id: Optional[str] = None,
        subprocess_pid: Optional[int] = None
    ) -> None:
        """Mark sync as started"""
        import os
        with self._state_lock:
            self._state.is_running = True
            self._state.sync_type = sync_type
            self._state.session_id = session_id
            self._state.start_time = datetime.datetime.now()
            self._state.list_type = list_type
            self._state.list_id = list_id
            self._state.pid = os.getpid()
            self._state.sync_subprocess_pid = subprocess_pid
            self._state.cancellation_requested = False
    
    def end_sync(self) -> None:
        """Mark sync as completed"""
        with self._state_lock:
            self._state.is_running = False
            self._state.sync_type = None
            self._state.session_id = None
            self._state.start_time = None
            self._state.list_type = None
            self._state.list_id = None
            self._state.pid = None
            self._state.sync_subprocess_pid = None
            self._state.cancellation_requested = False
    
    def set_subprocess_pid(self, pid: int) -> None:
        """Set the subprocess PID for the running sync (allows immediate termination)"""
        with self._state_lock:
            self._state.sync_subprocess_pid = pid
    
    def get_subprocess_pid(self) -> Optional[int]:
        """Get the subprocess PID if set"""
        with self._state_lock:
            return self._state.sync_subprocess_pid
    
    def get_state(self) -> Dict[str, Any]:
        """Get current sync state as dictionary"""
        with self._state_lock:
            state_dict = asdict(self._state)
            # Convert datetime to ISO format string
            if state_dict.get('start_time'):
                state_dict['start_time'] = self._state.start_time.isoformat()
            return state_dict
    
    def is_sync_running(self) -> bool:
        """Check if sync is currently running"""
        with self._state_lock:
            return self._state.is_running
    
    def get_sync_info(self) -> Optional[Dict[str, Any]]:
        """Get sync information if running, None otherwise"""
        with self._state_lock:
            if not self._state.is_running:
                return None
            
            info = {
                'sync_type': self._state.sync_type,
                'session_id': self._state.session_id,
                'start_time': self._state.start_time.isoformat() if self._state.start_time else None,
                'pid': self._state.pid,
                'sync_subprocess_pid': self._state.sync_subprocess_pid,
            }
            
            if self._state.sync_type == 'single':
                info['list_type'] = self._state.list_type
                info['list_id'] = self._state.list_id
            
            return info
    
    def request_cancellation(self) -> None:
        """Request cancellation of the current sync"""
        with self._state_lock:
            self._state.cancellation_requested = True
    
    def is_cancellation_requested(self) -> bool:
        """Check if cancellation has been requested"""
        with self._state_lock:
            return self._state.cancellation_requested
    
    def clear_cancellation(self) -> None:
        """Clear the cancellation request flag"""
        with self._state_lock:
            self._state.cancellation_requested = False


# Global instance for easy access
def get_sync_tracker() -> SyncStatusTracker:
    """Get the global sync status tracker instance"""
    return SyncStatusTracker()


# ---------------------------------------------
# Cross-process cancellation persistence
# ---------------------------------------------

_CANCEL_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cancel_requests.json")
_PAUSE_KEY = "pause_until"


def _ensure_cancel_file_dir():
    os.makedirs(os.path.dirname(_CANCEL_FILE), exist_ok=True)


def _read_cancel_requests() -> dict:
    try:
        if not os.path.exists(_CANCEL_FILE):
            return {}
        with open(_CANCEL_FILE, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_cancel_requests(data: dict):
    try:
        _ensure_cancel_file_dir()
        with open(_CANCEL_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        # Fail silently; the API still sets in-memory flag
        pass


def set_cancel_request(session_id: str):
    data = _read_cancel_requests()
    data[session_id] = {"cancel_requested": True}
    _write_cancel_requests(data)


def clear_cancel_request(session_id: str):
    data = _read_cancel_requests()
    if session_id in data:
        data.pop(session_id, None)
        _write_cancel_requests(data)


def is_cancel_requested_persisted(session_id: str) -> bool:
    data = _read_cancel_requests()
    entry = data.get(session_id)
    return bool(entry and entry.get("cancel_requested"))


# ---------------------------------------------
# Pause scheduling until a given timestamp
# ---------------------------------------------

def set_pause_until(timestamp_iso: str):
    data = _read_cancel_requests()
    data[_PAUSE_KEY] = timestamp_iso
    _write_cancel_requests(data)


def get_pause_until() -> Optional[str]:
    data = _read_cancel_requests()
    return data.get(_PAUSE_KEY)


def clear_pause_until():
    data = _read_cancel_requests()
    if _PAUSE_KEY in data:
        data.pop(_PAUSE_KEY, None)
        _write_cancel_requests(data)


# ---------------------------------------------
# Liveness of in-progress sync records
# ---------------------------------------------

def get_stale_timeout_seconds() -> int:
    """How long a sync record may go untouched before it counts as abandoned."""
    raw = os.getenv("LISTSYNC_SYNC_STALE_MINUTES")
    if raw:
        try:
            minutes = float(raw)
            if minutes > 0:
                return int(minutes * 60)
        except (TypeError, ValueError):
            logging.warning(f"Ignoring invalid LISTSYNC_SYNC_STALE_MINUTES={raw!r}")
    return DEFAULT_SYNC_STALE_SECONDS


def parse_db_timestamp(value: Any) -> Optional[datetime.datetime]:
    """
    Parse a sync_history timestamp into an aware UTC datetime.

    SQLite writes CURRENT_TIMESTAMP as naive UTC ("YYYY-MM-DD HH:MM:SS"), so a
    naive value has to be read as UTC. Reading it as local time would place
    every sync hours into the past or the future on any non-UTC deployment.
    """
    if not value:
        return None

    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = None
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = datetime.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def pid_is_alive(pid: int) -> Optional[bool]:
    """
    Whether a process exists, or None when that cannot be determined.

    psutil is only a dependency of the API server, so the core-only image falls
    back to a signal-0 probe rather than losing the check entirely.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return None

    # os.kill with signal 0 only probes on POSIX; on Windows it terminates.
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user
    except OSError:
        return None
    return True


def get_sync_last_activity(sync_record: Dict[str, Any]) -> Optional[datetime.datetime]:
    """Most recent sign of life for a sync record, as an aware UTC datetime."""
    if not sync_record:
        return None
    return (
        parse_db_timestamp(sync_record.get('last_heartbeat'))
        or parse_db_timestamp(sync_record.get('start_time'))
    )


def get_sync_staleness_reason(
    sync_record: Dict[str, Any],
    now: Optional[datetime.datetime] = None,
    stale_after_seconds: Optional[int] = None
) -> Optional[str]:
    """
    Explain why an in-progress sync record is abandoned, or None if it looks alive.

    Note that "the recorded PID is alive" is *not* evidence that the sync is
    running: full syncs record the PID of the long-lived core process, which
    outlives every sync it runs. Liveness therefore comes from the heartbeat,
    with a dead PID only used to spot an abandoned record sooner.

    Args:
        sync_record: A sync_history row (as a dict) with in_progress = 1
        now: Current time, aware UTC (defaults to now)
        stale_after_seconds: Override for the inactivity threshold

    Returns:
        str: Reason the record is stale, or None if the sync still looks alive
    """
    if not sync_record:
        return None

    if stale_after_seconds is None:
        stale_after_seconds = get_stale_timeout_seconds()
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    last_activity = get_sync_last_activity(sync_record)
    age_seconds = (now - last_activity).total_seconds() if last_activity else None

    pid = sync_record.get('pid')
    if pid and (age_seconds is None or age_seconds >= _MIN_AGE_BEFORE_PID_CHECK_SECONDS):
        if pid_is_alive(pid) is False:
            return f"process {pid} is no longer running"

    if age_seconds is not None and age_seconds > stale_after_seconds:
        return f"no sync activity for {int(age_seconds // 60)} minutes"

    return None


class SyncHeartbeat:
    """
    Keeps the sync_history row of a running sync marked as alive.

    Without it a sync that is killed mid-run leaves in_progress = 1 behind with
    no way to tell it apart from one that is still working.
    """

    def __init__(self, session_id: str, interval_seconds: int = SYNC_HEARTBEAT_INTERVAL_SECONDS):
        self.session_id = session_id
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "SyncHeartbeat":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name=f"sync-heartbeat-{self.session_id}",
            daemon=True
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        # Imported lazily so this module stays importable without the database.
        from ..database import heartbeat_sync_in_db

        while not self._stop_event.wait(self.interval_seconds):
            try:
                heartbeat_sync_in_db(self.session_id)
            except Exception as e:
                logging.warning(f"Sync heartbeat failed for {self.session_id}: {e}")

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None


def start_sync_heartbeat(
    session_id: str,
    interval_seconds: int = SYNC_HEARTBEAT_INTERVAL_SECONDS
) -> SyncHeartbeat:
    """Start heartbeating a running sync. Call stop() on the result when done."""
    return SyncHeartbeat(session_id, interval_seconds).start()
