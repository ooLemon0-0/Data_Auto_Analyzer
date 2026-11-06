"""Best-effort image retention; disk work never runs on the Qt event loop."""
from __future__ import annotations

import logging
from threading import Lock, Thread

from app.core.cancellation import CancellationToken, WorkCancelled, cancellation_scope, check_cancelled
from app.desktop.downloads import CacheInUse


logger = logging.getLogger(__name__)
CACHE_LIMIT_BYTES = 10 * 1024 ** 3
CACHE_CHECK_INTERVAL_MS = 60_000


class CacheMaintenance:
    def __init__(self, store, downloads, *, finished=lambda _result: None, limit=CACHE_LIMIT_BYTES):
        self.store, self.downloads = store, downloads
        self.finished, self.limit = finished, limit
        self.lock = Lock()
        self.token = CancellationToken()
        self.thread = None
        self.running = False

    def start(self):
        with self.lock:
            if self.running or self.token.cancelled:
                return False
            self.running = True
            self.thread = Thread(target=self._work, name="image-cache-cleanup", daemon=True)
            self.thread.start()
            return True

    def close(self):
        # Do not join: even a slow or unavailable disk cannot delay app exit.
        self.token.cancel()

    def trim(self):
        days = sorted(self.store.scan(), key=lambda entry: entry.day)
        total = sum(entry.size for entry in days)
        result = {"dates": [], "removed": 0, "freed": 0, "failed": 0, "remaining": total}
        for entry in days:
            check_cancelled()
            if total < self.limit:
                break
            try:
                # Claim and recheck under the download manager's lock. A review
                # or download started after the scan must still be protected.
                with self.downloads.cache_cleanup([entry.day], background=True):
                    report = self.store.clear([entry.day])
            except CacheInUse:
                continue
            except OSError:
                logger.warning("Could not clean image cache for %s; will retry later", entry.day, exc_info=True)
                continue
            result["dates"].append(entry.day)
            for key in ("removed", "freed", "failed"):
                result[key] += report[key]
            total = max(0, total - report["freed"])
        result["remaining"] = total
        return result

    def _work(self):
        try:
            with cancellation_scope(self.token):
                result = self.trim()
                check_cancelled()
            if result["removed"]:
                logger.info("Automatically cleared %s image cache files (%s bytes) for %s",
                    result["removed"], result["freed"], ", ".join(result["dates"]))
                self.finished(result)
        except WorkCancelled:
            pass
        except Exception:
            # A cleanup error must never become a foreground error dialog.
            logger.warning("Automatic image cache cleanup deferred", exc_info=True)
        finally:
            with self.lock:
                self.running = False
