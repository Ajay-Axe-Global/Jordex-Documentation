"""
shared/tracker.py — Tracking & State Persistence
=================================================
Shared across all 6 services. Uses file-level locking to prevent
concurrent write conflicts when multiple services run simultaneously.
"""
import json, os, logging
from datetime import datetime
from config import TRACKING_FILE

log = logging.getLogger("tracker")

try:
    from filelock import FileLock
    _LOCK = FileLock(TRACKING_FILE + ".lock")
except ImportError:
    # filelock not installed — use a no-op context manager
    from contextlib import contextmanager
    @contextmanager
    def _noop_lock():
        yield
    class _FakeLock:
        def __enter__(self): return _noop_lock().__enter__()
        def __exit__(self, *a): pass
    _LOCK = _FakeLock()
    log.warning("filelock not installed — concurrent writes may conflict. Run: pip install filelock")


class Tracker:
    def __init__(self, fp=TRACKING_FILE):
        self.fp = fp
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.fp):
            try:
                with open(self.fp) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save(self):
        with _LOCK:
            with open(self.fp, "w") as f:
                json.dump(self.data, f, indent=2, default=str)

    def reload(self):
        """Reload from disk — useful for long-running services to pick up changes."""
        with _LOCK:
            self.data = self._load()

    # Statuses that are terminal — never retry these
    _DONE_STATUSES = frozenset({
        "uploaded",
        "uploaded_needs_forward",
        "Skipped",
        "skipped_excel",
        "skipped_multi_attach",
        "no_attachment",
        "jordex_not_found",
        "failed",
    })

    def is_done(self, cat: str, conv_id: str) -> bool:
        """Return True if this email should not be reprocessed.

        Rules:
        - Terminal statuses (uploaded, skipped_*, failed, etc.) → always done.
        - 'downloaded' with retry_count == 0 → NOT done (allow one upload retry).
        - 'downloaded' with retry_count >= 1 → done (already retried once).
        """
        entry = self.data.get(cat, {}).get(conv_id)
        if entry is None:
            return False
        status = entry.get("status", "")
        if status in self._DONE_STATUSES:
            return True
        if status == "downloaded":
            return entry.get("retry_count", 0) >= 1
        # Any other unknown status → treat as done (safe default)
        return True

    def mark(self, cat: str, conv_id: str, subject: str, folder_name: str,
             files: list, status: str, mbl: str = None, secondary_ref: str = None, **kwargs):
        data = {
            "subject": subject,
            "folder_name": folder_name,
            "files": files,
            "mbl": mbl,
            "secondary_ref": secondary_ref,
            "processed_at": datetime.now().isoformat(),
            "status": status,
            "retry_count": 0,
        }
        data.update(kwargs)
        with _LOCK:
            self.reload()
            existing = self.data.setdefault(cat, {}).get(conv_id)
            # If already present as 'downloaded', increment retry_count
            if existing and existing.get("status") == "downloaded" and status == "downloaded":
                data["retry_count"] = existing.get("retry_count", 0) + 1
                log.info("tracker: retry_count=%d for %s/%s", data["retry_count"], cat, conv_id)
            self.data[cat][conv_id] = data
            self.save()

    def update_status(self, cat: str, conv_id: str, status: str):
        with _LOCK:
            self.reload()
            if conv_id in self.data.get(cat, {}):
                self.data[cat][conv_id]["status"] = status
                self.save()

    def stats(self, cat: str) -> dict:
        c = self.data.get(cat, {})
        return {
            "total":         len(c),
            "downloaded":    sum(1 for v in c.values() if v["status"] == "downloaded"),
            "uploaded":      sum(1 for v in c.values() if v.get("status") == "uploaded"),
            "no_attachment": sum(1 for v in c.values() if v["status"] == "no_attachment"),
            "failed":        sum(1 for v in c.values() if v["status"] == "failed"),
        }

    def all_stats(self) -> dict:
        from config import LABELS
        return {svc: self.stats(cat) for _, cat, _, svc in LABELS}
