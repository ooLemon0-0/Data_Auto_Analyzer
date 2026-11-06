"""Independent point jobs; downloading never owns the foreground review lock."""
from __future__ import annotations

from datetime import date, datetime
from contextlib import contextmanager
import json
import logging
from pathlib import Path
from threading import RLock
import time
from urllib.parse import urlsplit
import uuid

from app import db
from app.core.progress import progress_scope, report_progress
from app.core.cancellation import CancellationToken, WorkCancelled, cancel_pause, cancellable_lock, check_cancelled, cancellation_scope
from app.core.executor import DaemonExecutor
from app.services.review_service import ReviewService


logger = logging.getLogger(__name__)
ACTIVE = {"queued", "running", "stopping"}


class CacheInUse(RuntimeError):
    pass


def task_key(project_id: str, business_date: date | str):
    return f"{project_id}:{business_date}"


class DownloadManager:
    def __init__(self, projects, changed, *, service=None, max_projects=4, ensure_route=None, state_path=None):
        self.projects = {p.id: p for p in projects}
        self.changed = changed
        self.service = service or ReviewService()
        self.ensure_route = ensure_route or self._ensure_route
        self.limit = max_projects
        self.lock = RLock()
        self.pool = DaemonExecutor(max_workers=max_projects, thread_name_prefix="point-fetch")
        self.jobs = {}
        self.futures = set()
        self.closed = False
        self.cleaning_dates = set()
        self.background_cleaning_dates = set()
        self.review_dates = set()
        self._route_locks = {}
        self.state_path = Path(state_path) if state_path else None

    @staticmethod
    def _host(project):
        source = project.source
        return urlsplit(source.get("base_url") or source.get("history_url") or project.id).netloc or project.id

    @staticmethod
    def _public(job):
        return {k: v for k, v in job.items() if k not in {"cache", "attempt", "host", "token"}}

    def _save(self):
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".json.part")
            temporary.write_text(json.dumps({"version": 1, "jobs": [self._public(j) for j in self.jobs.values()]}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError:
            logger.warning("Could not persist download task state", exc_info=True)

    def snapshots(self):
        with self.lock:
            return [self._public(j) for j in self.jobs.values()]

    def get(self, key):
        with self.lock:
            return self._public(self.jobs[key])

    def has_active(self):
        with self.lock:
            return any(j["status"] in ACTIVE for j in self.jobs.values())

    def submit(self, project_ids, business_date: date, target: int):
        """Deduplicate by project/day, so two targets cannot edit one queue together."""
        if target < 1:
            raise ValueError("有效样本数必须大于零")
        cache, published = {}, []
        with self.lock:
            if self.closed:
                raise RuntimeError("下载调度已关闭")
            if str(business_date) in self.cleaning_dates - self.background_cleaning_dates:
                raise RuntimeError("该日期的图片缓存正在清理，请稍后再拉取")
            for project_id in dict.fromkeys(project_ids):
                project = self.projects[project_id]
                key = task_key(project_id, business_date)
                old = self.jobs.get(key)
                if old and (old["status"] in ACTIVE or (old["status"] == "ready" and old["target"] == target
                        and str(business_date) not in self.cleaning_dates)):
                    continue
                job = {"key": key, "project_id": project_id, "name": project.name,
                       "business_date": str(business_date), "target": target, "status": "queued",
                       "stage": "queued", "message": "等待并发任务空位", "current": None, "total": None,
                       "error": "", "host": self._host(project), "attempt": uuid.uuid4().hex, "cache": cache,
                       "token": CancellationToken(), "created_at": time.time(), "started_at": None,
                       "updated_at": time.time(), "finished_at": None,
                       "records_current": None, "records_total": None,
                       "images_ready": old.get("images_ready", 0) if old else 0,
                       "images_total": old.get("images_total") if old and old["target"] == target else None}
                self.jobs[key] = job
                published.append(self._public(job))
            self._save()
        for job in published:
            self.changed(job)
        self._pump()
        return [j["key"] for j in published]

    def restore_sessions(self, limit=None):
        """Restore local review tasks without logging in or touching the network."""
        restored = []
        with db.connect() as conn:
            query = "SELECT * FROM review_sessions ORDER BY updated_at DESC"
            sessions = conn.execute(query + " LIMIT ?", (limit,)).fetchall() if limit else conn.execute(query).fetchall()
            for index, session in enumerate(sessions, 1):
                from app.desktop.startup import report_startup
                report_startup("恢复本地审核任务", index, len(sessions))
                project = self.projects.get(session["project_id"])
                if project is None:
                    continue
                rows = conn.execute("""SELECT i.* FROM review_queue q JOIN items i ON i.id=q.item_id
                    WHERE q.session_id=? ORDER BY q.seq""", (session["id"],)).fetchall()
                if not rows:
                    continue
                revision = self.service.image_cache_revision({"source": project.source})
                count = sum(self.service._existing_image(r, revision) is not None for r in rows)
                record_count = conn.execute("SELECT COUNT(*) FROM items WHERE project_id=? AND business_date=?",
                    (project.id, session["business_date"])).fetchone()[0]
                key = task_key(project.id, session["business_date"])
                try:
                    created = datetime.fromisoformat(session["created_at"]).timestamp()
                    updated = datetime.fromisoformat(session["updated_at"]).timestamp()
                except (TypeError, ValueError):
                    created = updated = time.time()
                job = {"key": key, "project_id": project.id, "name": project.name,
                       "business_date": session["business_date"], "target": session["target_size"],
                       "status": "ready" if count == len(rows) else "paused", "stage": "images",
                       "message": "已从本地恢复，可以继续审核" if count == len(rows) else "点击补齐端面框复核图，已有原图和审核结果保留" if revision else "本地缓存不完整，点击重试补齐",
                       "current": count, "total": len(rows), "error": "", "host": self._host(project),
                       "attempt": uuid.uuid4().hex, "cache": None, "token": CancellationToken(),
                       "created_at": created, "started_at": updated, "updated_at": updated, "finished_at": updated,
                       "images_ready": count, "images_total": len(rows), "records_current": record_count, "records_total": record_count}
                with self.lock:
                    self.jobs.setdefault(key, job)
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8")).get("jobs", [])
                for old in saved:
                    project = self.projects.get(old.get("project_id"))
                    if project is None:
                        continue
                    day = date.fromisoformat(old["business_date"])
                    target = int(old["target"])
                    if target < 1:
                        continue
                    key = task_key(project.id, day)
                    current = self.jobs.get(key)
                    if current and current["status"] == "ready" and current["target"] == target:
                        for field in ("created_at", "started_at", "updated_at", "finished_at"):
                            if old.get(field) is not None:
                                current[field] = old[field]
                        continue
                    job = {**old, "key": key, "name": project.name, "target": target,
                           "host": self._host(project), "attempt": uuid.uuid4().hex,
                           "cache": None, "token": CancellationToken()}
                    if old.get("status") in ACTIVE:
                        job.update(status="cancelled", message="上次拉取已中断，点击继续；已完成缓存保留",
                            finished_at=old.get("updated_at") or old.get("created_at") or time.time())
                    elif old.get("status") == "ready":
                        job.update(status="paused", message="缓存需要补齐，点击继续拉取")
                    if current:
                        job.update(images_ready=current["images_ready"], images_total=current["images_total"])
                    self.jobs[key] = job
            except (OSError, ValueError, TypeError, KeyError):
                logger.warning("Could not restore download task state", exc_info=True)
        restored = self.snapshots()
        with self.lock:
            self._save()
        for job in restored:
            self.changed(job)
        return restored

    def _pump(self):
        with self.lock:
            if self.closed:
                return
            running = [j for j in self.jobs.values() if j["status"] in {"running", "stopping"}]
            hosts = {j["host"] for j in running}
            slots = self.limit - len(running)
            # One job per edge box; distinct boxes proceed together. Points on
            # one box reuse their batch's validated history after the first job.
            for job in self.jobs.values():
                if slots <= 0:
                    break
                if job["status"] != "queued" or job["host"] in hosts or job["business_date"] in self.cleaning_dates:
                    continue
                job.update(status="running", stage="network", message="正在连接数据服务", started_at=time.time(), updated_at=time.time())
                hosts.add(job["host"])
                slots -= 1
                self.changed(self._public(job))
                future = self.pool.submit(self._work, job["key"], job["attempt"], job["cache"])
                self.futures.add(future)
                future.add_done_callback(self._future_done)
            for job in self.jobs.values():
                if job["status"] == "queued":
                    blocker = next((j for j in self.jobs.values() if j["host"] == job["host"] and j["status"] in {"running", "stopping"}), None)
                    job["message"] = ("正在释放旧图片缓存，随后自动拉取" if job["business_date"] in self.cleaning_dates else
                        f"等待同一设备上的“{blocker['name']}”结束后自动开始" if blocker else f"已有 {len(hosts)} 个点位在准备，完成一个后自动开始")
                    self.changed(self._public(job))
            self._save()

    def _future_done(self, future):
        with self.lock:
            self.futures.discard(future)

    def _progress(self, key, attempt, event):
        with self.lock:
            job = self.jobs.get(key)
            if job is None or job["attempt"] != attempt or job["status"] != "running":
                return
            job.update(event)
            job["updated_at"] = time.time()
            if event.get("stage") == "records":
                job.update(records_current=event.get("current"), records_total=event.get("total"))
            elif event.get("stage") == "images":
                job.update(images_ready=event.get("current") or 0, images_total=event.get("total"))
            snapshot = self._public(job)
        self.changed(snapshot)

    def _work(self, key, attempt, cache):
        with self.lock:
            job = self._public(self.jobs[key])
            token = self.jobs[key]["token"]
        try:
            with progress_scope(lambda event: self._progress(key, attempt, event), collection_cache=cache, cancellation_token=token):
                check_cancelled()
                self.ensure_route(self.projects[job["project_id"]])
                state = self.service.prepare(job["project_id"], date.fromisoformat(job["business_date"]), job["target"])
                check_cancelled()
            status, message, error = "ready", "图片已就绪，可以开始审核", ""
        except Exception as exc:
            if token.cancelled or isinstance(exc, WorkCancelled):
                status, message, error = "cancelled", "已停止，已完成缓存和审核结果保留；可继续拉取", ""
            else:
                logger.exception("Point download failed: %s", job["project_id"])
                status, message, error = "failed", "拉取未完成，已缓存的图片会保留", str(exc)
        with self.lock:
            live = self.jobs.get(key)
            if live is None or live["attempt"] != attempt:
                return
            live.update(status=status, message=message, error=error, cache=None, finished_at=time.time(), updated_at=time.time())
            if status == "ready":
                count = state.get("local_images_ready", state.get("queue_length", job["target"])) if isinstance(state, dict) else job["target"]
                live.update(images_ready=count, images_total=count)
            if not self.closed:
                self._save()
            snapshot = self._public(live)
        if not self.closed:
            self.changed(snapshot)
        self._pump()

    def _ensure_route(self, project):
        connection = project.source.get("remote_connection_id")
        if not connection:
            return
        with self.lock:
            lock = self._route_locks.setdefault(connection, RLock())
        with cancellable_lock(lock):
            from app.remote_access.service import remote_access_service
            report_progress("network", "正在连接瑞丰远程通道，请保持远程桌面连接")
            status = remote_access_service.launch(connection)
            deadline = time.monotonic() + 180
            while status.get("phase") not in ("ready", "error", "needs_setup") and time.monotonic() < deadline:
                cancel_pause(2)
                status = remote_access_service.status(connection)
            if not status.get("proxy_reachable"):
                raise RuntimeError(status.get("message") or "瑞丰远程通道尚未就绪")

    def cancel(self, key=None):
        snapshots, tokens = [], []
        with self.lock:
            for job in self.jobs.values():
                if (key is None or job["key"] == key) and job["status"] in ACTIVE:
                    tokens.append(job["token"])
                    if job["status"] == "queued":
                        job.update(status="cancelled", cache=None, message="已停止排队；可继续拉取", finished_at=time.time())
                    else:
                        job.update(status="stopping", message="正在停止当前请求，已完成缓存保留")
                    job["updated_at"] = time.time()
                    snapshots.append(self._public(job))
            self._save()
        for token in tokens:
            token.cancel()
        for job in snapshots:
            self.changed(job)
        return len(snapshots)

    def protect_review_dates(self, days):
        """Claim dates before opening images; never wait for a disk cleanup."""
        selected = set(days)
        with self.lock:
            if selected & self.cleaning_dates:
                return False
            self.review_dates = selected
            return True

    @contextmanager
    def cache_cleanup(self, days, *, background=False):
        selected = {date.fromisoformat(day).isoformat() for day in days}
        with self.lock:
            if any(j["business_date"] in selected and j["status"] in ACTIVE for j in self.jobs.values()):
                raise CacheInUse("所选日期仍有拉取任务，请先停止这些任务，再清理缓存")
            if selected & self.cleaning_dates:
                raise CacheInUse("这些日期正在清理，请稍后刷新")
            if background and selected & self.review_dates:
                raise CacheInUse("该日期正在审核，已跳过自动清理")
            self.cleaning_dates.update(selected)
            if background:
                self.background_cleaning_dates.update(selected)
        try:
            yield
        finally:
            try:
                # Reconcile even when a cleanup was interrupted partway through.
                with cancellation_scope(None):
                    self.refresh_cached_dates(selected)
            finally:
                with self.lock:
                    self.cleaning_dates.difference_update(selected)
                    self.background_cleaning_dates.difference_update(selected)
                self._pump()

    def refresh_cached_dates(self, days):
        if self.closed:
            return
        jobs = [j for j in self.snapshots() if j["business_date"] in days]
        snapshots = []
        for job in jobs:
            project = self.projects[job["project_id"]]
            revision = self.service.image_cache_revision({"source": project.source})
            with db.connect() as conn:
                rows = conn.execute("""SELECT i.* FROM review_sessions s JOIN review_queue q ON q.session_id=s.id
                    JOIN items i ON i.id=q.item_id WHERE s.project_id=? AND s.business_date=?""",
                    (job["project_id"], job["business_date"])).fetchall()
            count = sum(self.service._existing_image(row, revision) is not None for row in rows)
            with self.lock:
                if self.closed:
                    return
                live = self.jobs.get(job["key"])
                if live is None or live["status"] in ACTIVE:
                    continue
                live.update(images_ready=count, images_total=len(rows) or live.get("images_total"), current=count,
                            updated_at=time.time())
                if rows and count < len(rows):
                    live.update(status="paused", stage="images", message="图片缓存已清理，点击继续拉取；审核结果保留")
                snapshots.append(self._public(live))
        with self.lock:
            if not self.closed:
                self._save()
        for job in snapshots:
            self.changed(job)

    def shutdown(self, *, cancel=False):
        with self.lock:
            if self.has_active() and not cancel:
                raise RuntimeError("仍有后台下载任务")
            self.closed = True
        if cancel:
            self.cancel()
        self.pool.shutdown(wait=False, cancel_futures=cancel)
