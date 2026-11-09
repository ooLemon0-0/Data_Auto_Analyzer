from __future__ import annotations

import json
import hashlib
import random
from concurrent.futures import as_completed
from contextvars import copy_context
from datetime import date, datetime
from pathlib import Path
from typing import Any
from threading import BoundedSemaphore, RLock
from urllib.parse import urlparse

from app import db
from app.core.config import settings
from app.core.registry import SOURCE_FACTORIES, build_sink, build_source
from app.core.progress import report_progress
from app.core.cancellation import check_cancelled, cancellable_lock, WorkCancelled
from app.core.executor import DaemonExecutor


# A batch may have several project pools. Bound aggregate requests as well.
_IMAGE_SLOTS = BoundedSemaphore(8)
_INDEX_LOCK = RLock()
_INDEX_LOCKS = {}


class ReviewService:
    def project_dict(self, project_id: str) -> dict[str, Any]:
        return settings.project(project_id).model_dump()

    @staticmethod
    def image_cache_revision(project):
        return getattr(SOURCE_FACTORIES.get(project.get("source", {}).get("type")), "image_cache_revision", "")

    @staticmethod
    def _stable_order(ids: list[int], project_id: str, business_date: str) -> list[int]:
        ordered = list(ids)
        random.Random(f"{project_id}:{business_date}").shuffle(ordered)
        return ordered

    def _ensure_index(self, project, business_date, target=0):
        revision = self.image_cache_revision(project)
        key = (project["id"], business_date.isoformat())
        with _INDEX_LOCK:
            lock = _INDEX_LOCKS.setdefault(key, RLock())
        with cancellable_lock(lock):
            with db.connect() as conn:
                rows = conn.execute("SELECT metadata_json FROM items WHERE project_id=? AND business_date=?", key).fetchall()
            outdated = bool(revision) and any(json.loads(r["metadata_json"] or "{}").get("review_image_revision") != revision for r in rows)
            refresh = bool(project.get("source", {}).get("refresh_on_prepare", False))
            if not rows or len(rows) < target or refresh or outdated:
                if outdated:
                    report_progress("records", "正在补齐端面框坐标，已有原图和审核结果会保留")
                items = build_source(project).fetch_day(business_date)
                db.upsert_items(project["id"], business_date.isoformat(), items)
            else:
                report_progress("records", f"本地已有 {len(rows):,} 条记录，无需重新查询", len(rows), len(rows))

    def prepare(self, project_id: str, business_date: date, target_size: int | None = None) -> dict[str, Any]:
        report_progress("records", "正在检查该日期的本地数据")
        project = self.project_dict(project_id)
        target = int(target_size or project["daily_target"])
        if target < 1:
            raise ValueError("target_size must be >= 1")

        # Re-opening an existing day should be instant. Fetch from the intranet only if the day
        # has not been indexed yet, or config explicitly requests refresh.
        self._ensure_index(project, business_date, target)

        report_progress("sampling", "正在恢复审核进度并生成抽样队列")

        with db.connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO review_sessions(project_id, business_date, target_size, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, business_date) DO UPDATE SET
                    target_size=excluded.target_size,
                    updated_at=excluded.updated_at
                """,
                (project_id, business_date.isoformat(), target, now, now),
            )
            session = conn.execute(
                "SELECT * FROM review_sessions WHERE project_id=? AND business_date=?",
                (project_id, business_date.isoformat()),
            ).fetchone()

            existing_count = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE session_id=?", (session["id"],)
            ).fetchone()[0]

            if existing_count == 0:
                item_rows = conn.execute(
                    "SELECT id FROM items WHERE project_id=? AND business_date=? ORDER BY id",
                    (project_id, business_date.isoformat()),
                ).fetchall()
                ids = self._stable_order(
                    [row["id"] for row in item_rows], project_id, business_date.isoformat()
                )
                if len(ids) < target:
                    raise RuntimeError(
                        f"当天只有 {len(ids)} 条可用记录，但目标有效样本数是 {target}。"
                    )
                for seq, item_id in enumerate(ids[:target], start=1):
                    conn.execute(
                        "INSERT INTO review_queue(session_id, item_id, seq) VALUES (?, ?, ?)",
                        (session["id"], item_id, seq),
                    )
            else:
                self._rebalance_queue(conn, session)

        prefetch = None
        if project.get("source", {}).get("prefetch_sample_images", False):
            prefetch = self._prefetch_queue_images(project, business_date)

        state = self.state(project_id, business_date)
        if prefetch is not None:
            state["local_images_ready"] = prefetch["ready"]
            state["local_images_downloaded"] = prefetch["downloaded"]
        report_progress("ready", "样本图片已保存到本机，可以开始审核", 1, 1)
        return state

    @staticmethod
    def _image_destination(row, revision="") -> Path:
        image_url = (row["image_url"] or "").strip()
        suffix = Path(urlparse(image_url).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}:
            suffix = ".jpg"
        return (
            Path(settings.storage.data_root).resolve()
            / row["project_id"]
            / row["business_date"]
            / "images"
            / (f"{row['id']}.{revision}.png" if revision else f"{row['id']}{suffix}")
        )

    @staticmethod
    def _existing_image(row, revision="") -> Path | None:
        image_path = (row["image_path"] or "").strip()
        if image_path:
            path = Path(image_path).resolve()
            if path.exists() and (not revision or path.name.endswith(f".{revision}.png")):
                return path
        destination = ReviewService._image_destination(row, revision)
        return destination if destination.exists() else None

    def _cache_image_row(self, row, source) -> tuple[Path, bool]:
        check_cancelled()
        revision = getattr(source, "image_cache_revision", "")
        existing = self._existing_image(row, revision)
        metadata = json.loads(row["metadata_json"] or "{}") if "metadata_json" in row.keys() else {}
        if existing is not None:
            path = existing
            downloaded = False
        else:
            image_url = (row["image_url"] or "").strip()
            if not image_url:
                raise RuntimeError(f"样本 {row['id']} 没有现场图片地址")
            with cancellable_lock(_IMAGE_SLOTS):
                destination = self._image_destination(row, revision)
                if revision:
                    path, patch = source.materialize_review_image(image_url, destination, metadata,
                        original=metadata.get("original_cache_path") or row["image_path"], label=row["recognition_text"])
                    metadata.update(patch)
                else:
                    path = source.materialize_image(image_url, destination)
            downloaded = True

        with db.connect() as conn:
            conn.execute(
                "UPDATE items SET image_path=? WHERE id=?",
                (str(path.resolve()), row["id"]),
            )
            if revision:
                conn.execute("UPDATE items SET metadata_json=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), row["id"]))
        return path, downloaded

    def _prefetch_queue_images(
        self, project: dict[str, Any], business_date: date
    ) -> dict[str, int]:
        """Materialize the current review sample before returning it to the UI."""
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT i.id, i.project_id, i.business_date, i.image_path, i.image_url, i.metadata_json, i.recognition_text
                FROM review_queue q
                JOIN review_sessions s ON s.id=q.session_id
                JOIN items i ON i.id=q.item_id
                WHERE s.project_id=? AND s.business_date=?
                ORDER BY q.seq
                """,
                (project["id"], business_date.isoformat()),
            ).fetchall()

        source = build_source(project)
        revision = getattr(source, "image_cache_revision", "")
        pending = [row for row in rows if self._existing_image(row, revision) is None]
        report_progress("images", f"本地图片 {len(rows) - len(pending)} / {len(rows)} 张", len(rows) - len(pending), len(rows))
        if not pending:
            return {"ready": len(rows), "downloaded": 0}

        downloaded = 0
        failures: list[str] = []
        workers = min(4, max(1, int(project.get("source", {}).get("image_download_workers", 4)))) if getattr(source, "parallel_image_downloads", False) else 1
        # Browser-backed legacy sources retain a single worker/profile. HTTP
        # sources are stateless and can materialize independent files in parallel.
        with DaemonExecutor(max_workers=workers, thread_name_prefix="sample-image") as pool:
            futures = {pool.submit(copy_context().run, self._cache_image_row, row, source): row["id"] for row in pending}
            for index, future in enumerate(as_completed(futures), 1):
                check_cancelled()
                try:
                    _, was_downloaded = future.result()
                    downloaded += int(was_downloaded)
                except WorkCancelled:
                    raise
                except Exception as exc:
                    check_cancelled()
                    failures.append(f"{futures[future]}: {exc}")
                ready = len(rows) - len(pending) + index - len(failures)
                report_progress("images", f"已缓存 {ready} / {len(rows)} 张图片", ready, len(rows))

        if failures:
            preview = "；".join(failures[:3])
            extra = f"；另有 {len(failures) - 3} 条" if len(failures) > 3 else ""
            raise RuntimeError(
                f"抽样图片预缓存失败 {len(failures)} 条（{preview}{extra}），请重试拉取"
            )
        return {"ready": len(rows), "downloaded": downloaded}

    def materialize_item_image(self, item_id: int) -> Path:
        """Return a cached image, fetching it only when an uncached item is opened."""
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT id, project_id, business_date, image_path, image_url, metadata_json, recognition_text
                FROM items WHERE id=?
                """,
                (item_id,),
            ).fetchone()
        if not row:
            raise KeyError("Item not found")

        project = self.project_dict(row["project_id"])
        revision = self.image_cache_revision(project)
        if revision and json.loads(row["metadata_json"] or "{}").get("review_image_revision") != revision:
            self._ensure_index(project, date.fromisoformat(row["business_date"]))
            with db.connect() as conn:
                row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        existing = self._existing_image(row, revision)
        if existing is not None:
            return existing
        path, _ = self._cache_image_row(row, build_source(project))
        return path

    def _session(self, conn, project_id: str, business_date: date):
        session = conn.execute(
            "SELECT * FROM review_sessions WHERE project_id=? AND business_date=?",
            (project_id, business_date.isoformat()),
        ).fetchone()
        if not session:
            raise RuntimeError("Review session has not been prepared yet")
        return session

    def materialize_original_image(self, item_id: int) -> Path:
        """Require an explicit full-frame source. Never export a review/crop image."""
        with db.connect() as conn:
            row = conn.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise KeyError('未找到原始记录')
        metadata = json.loads(row['metadata_json'] or '{}')
        url = str(metadata.get('original_image_url') or '').strip()
        project = self.project_dict(row['project_id'])
        if not url:
            # Older indices may only contain a crop URL. Refresh metadata by
            # source key, retaining the existing queue and saved decisions.
            db.upsert_items(row['project_id'], row['business_date'],
                build_source(project).fetch_day(date.fromisoformat(row['business_date'])))
            with db.connect() as conn:
                row = conn.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            metadata = json.loads(row['metadata_json'] or '{}')
            url = str(metadata.get('original_image_url') or '').strip()
        if not url:
            raise RuntimeError('此记录没有可确认的完整原图地址，不能用框图或局部图替代。请重新拉取该点位后再试。')
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}:
            suffix = '.jpg'
        images = Path(settings.storage.data_root).resolve() / row['project_id'] / row['business_date'] / 'images'
        name = hashlib.sha256(url.encode()).hexdigest() + suffix
        # Phase 2 stores its unmodified full frame separately from the overlay.
        known_raw = images / 'originals' / name
        destination = images / 'export-originals' / name
        with _INDEX_LOCK:
            lock = _INDEX_LOCKS.setdefault(('original', str(destination)), RLock())
        with cancellable_lock(lock):
            path = known_raw if known_raw.is_file() else destination
            if not path.is_file():
                with cancellable_lock(_IMAGE_SLOTS):
                    path = Path(build_source(project).materialize_image(url, destination))
            from PIL import Image
            with Image.open(path) as original:
                original.verify()
            return path

    def _rebalance_queue(self, conn, session) -> None:
        target = int(session["target_size"])

        def rows():
            return conn.execute(
                "SELECT id, item_id, seq, decision FROM review_queue WHERE session_id=? ORDER BY seq",
                (session["id"],),
            ).fetchall()

        current = rows()
        valid_count = sum(r["decision"] in ("correct", "incorrect") for r in current)

        # If an earlier invalid record is changed back to valid after its replacement was already
        # reviewed, remove the newest replacement result so the effective sample denominator stays
        # exactly equal to target. Invalid rows themselves are retained as audit history.
        if valid_count > target:
            excess = valid_count - target
            removable = [r for r in reversed(current) if r["decision"] in ("correct", "incorrect")]
            for row in removable[:excess]:
                conn.execute("DELETE FROM review_queue WHERE id=?", (row["id"],))

        current = rows()
        valid_count = sum(r["decision"] in ("correct", "incorrect") for r in current)
        pending = [r for r in current if r["decision"] is None]
        needed_pending = max(0, target - valid_count)

        # Target reductions or invalid->valid corrections can leave unused pending replacements.
        if len(pending) > needed_pending:
            for row in list(reversed(pending))[: len(pending) - needed_pending]:
                conn.execute("DELETE FROM review_queue WHERE id=?", (row["id"],))

        current = rows()
        valid_count = sum(r["decision"] in ("correct", "incorrect") for r in current)
        pending_count = sum(r["decision"] is None for r in current)
        needed = max(0, target - valid_count - pending_count)
        if needed == 0:
            return

        current_max = max((r["seq"] for r in current), default=0)
        all_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM items WHERE project_id=? AND business_date=? ORDER BY id",
                (session["project_id"], session["business_date"]),
            ).fetchall()
        ]
        queued = {r["item_id"] for r in current}
        candidates = [
            item_id
            for item_id in self._stable_order(all_ids, session["project_id"], session["business_date"])
            if item_id not in queued
        ][:needed]

        if len(candidates) < needed:
            raise RuntimeError("无效样本需要补抽，但当天剩余数据已经不足。")
        for offset, item_id in enumerate(candidates, start=1):
            conn.execute(
                "INSERT INTO review_queue(session_id, item_id, seq) VALUES (?, ?, ?)",
                (session["id"], item_id, current_max + offset),
            )

    def decide(self, project_id: str, business_date: date, queue_id: int, decision: str) -> dict[str, Any]:
        if decision not in {"correct", "incorrect", "invalid", "other"}:
            raise ValueError(f"Unsupported decision: {decision}")
        with db.connect() as conn:
            session = self._session(conn, project_id, business_date)
            row = conn.execute(
                "SELECT * FROM review_queue WHERE id=? AND session_id=?",
                (queue_id, session["id"]),
            ).fetchone()
            if not row:
                raise KeyError("Queue item not found")
            conn.execute(
                "UPDATE review_queue SET decision=?, reviewed_at=? WHERE id=?",
                (decision, datetime.now().isoformat(timespec="seconds"), queue_id),
            )
            self._rebalance_queue(conn, session)
            conn.execute('UPDATE review_sessions SET updated_at=? WHERE id=?',
                (datetime.now().isoformat(timespec='seconds'), session['id']))
        return self.state(project_id, business_date, focus_queue_id=queue_id, move="next")

    def state(
        self,
        project_id: str,
        business_date: date,
        focus_queue_id: int | None = None,
        move: str | None = None,
    ) -> dict[str, Any]:
        with db.connect() as conn:
            session = self._session(conn, project_id, business_date)
            rows = conn.execute(
                """
                SELECT q.id AS queue_id, q.seq, q.decision, q.reviewed_at,
                       i.id AS item_id, i.source_key, i.image_path, i.image_url AS source_image_url,
                       i.recognition_text, i.metadata_json
                FROM review_queue q
                JOIN items i ON i.id=q.item_id
                WHERE q.session_id=? ORDER BY q.seq
                """,
                (session["id"],),
            ).fetchall()
            session_dict = dict(session)

        entries = [dict(row) for row in rows]
        for entry in entries:
            entry["metadata"] = json.loads(entry.pop("metadata_json") or "{}")
            entry["image_url"] = f"/api/items/{entry['item_id']}/image"
            entry.pop("image_path", None)
            entry.pop("source_image_url", None)

        correct = sum(e["decision"] == "correct" for e in entries)
        incorrect = sum(e["decision"] == "incorrect" for e in entries)
        invalid = sum(e["decision"] == "invalid" for e in entries)
        other = sum(e["decision"] == "other" for e in entries)
        valid = correct + incorrect
        target = int(session_dict["target_size"])
        complete = valid >= target

        current_index = 0
        if entries:
            if focus_queue_id is not None:
                idx = next((i for i, e in enumerate(entries) if e["queue_id"] == focus_queue_id), 0)
                if move == "next":
                    if complete:
                        current_index = idx
                    else:
                        next_unreviewed = next(
                            (i for i in range(idx + 1, len(entries)) if entries[i]["decision"] is None),
                            None,
                        )
                        current_index = next_unreviewed if next_unreviewed is not None else idx
                elif move == "previous":
                    current_index = max(0, idx - 1)
                else:
                    current_index = idx
            else:
                current_index = next((i for i, e in enumerate(entries) if e["decision"] is None), len(entries) - 1)

        accuracy = (correct / valid) if valid else None
        uploaded = bool(session_dict.get("uploaded_at"))
        return {
            "project_id": project_id,
            "business_date": business_date.isoformat(),
            "target_size": target,
            "correct": correct,
            "incorrect": incorrect,
            "invalid": invalid,
            "other": other,
            "valid_count": valid,
            "accuracy": accuracy,
            "complete": complete,
            "uploaded": uploaded,
            "uploaded_at": session_dict.get("uploaded_at"),
            "queue_length": len(entries),
            "current_index": current_index,
            "current": entries[current_index] if entries else None,
            "entries": entries,
        }

    def navigate(self, project_id: str, business_date: date, queue_id: int, direction: str) -> dict[str, Any]:
        with db.connect() as conn:
            session = self._session(conn, project_id, business_date)
            rows = conn.execute(
                "SELECT id FROM review_queue WHERE session_id=? ORDER BY seq",
                (session["id"],),
            ).fetchall()
        ids = [row["id"] for row in rows]
        if queue_id not in ids:
            raise KeyError("Queue item not found")
        idx = ids.index(queue_id)
        if direction == "previous":
            target_idx = max(0, idx - 1)
        elif direction == "next":
            target_idx = min(len(ids) - 1, idx + 1)
        else:
            raise ValueError("direction must be previous or next")
        return self.state(project_id, business_date, focus_queue_id=ids[target_idx])

    def prepare_sink_auth(self, project_id: str) -> dict[str, Any]:
        project = self.project_dict(project_id)
        sink_cfg = project.get("sink", {})
        if not sink_cfg.get("enabled", False):
            raise RuntimeError("该项目没有启用上传 Sink。")
        return build_sink(project).prepare_auth()

    def upload(
        self,
        project_id: str,
        business_date: date,
    ) -> dict[str, Any]:

        project = self.project_dict(
            project_id
        )

        state = self.state(
            project_id,
            business_date,
        )

        # ============================================================
        # 1. 只有审核完成才允许上传
        # ============================================================

        if not state["complete"]:

            raise RuntimeError(
                "审核尚未完成："
                f"{state['valid_count']}/"
                f"{state['target_size']} 条有效样本。"
            )

        # ============================================================
        # 2. 生成当前最新统计
        #
        # 注意：
        # 不再检查 uploaded_at 阻止再次上传。
        #
        # 每一次点击上传，
        # 都重新根据当前审核状态生成 summary，
        # 然后交给 Sink 做日期 upsert。
        # ============================================================

        summary = {
            "sample_count":
                state["valid_count"],

            "correct":
                state["correct"],

            "incorrect":
                state["incorrect"],

            "invalid":
                state["invalid"],

            "other": state["other"],

            "accuracy":
                round(
                    (
                        state["accuracy"]
                        or 0
                    )
                    * 100,
                    2,
                ),
        }

        # ============================================================
        # 3. 每次都真正调用 Sink
        #
        # Sink 当前语义：
        #
        #   当天不存在 -> 新增
        #   当天已存在 -> 覆盖
        #   然后排序
        #   然后回读校验
        # ============================================================

        result = (
            build_sink(
                project
            )
            .upload_day(
                business_date,
                summary,
            )
        )

        # ============================================================
        # 4. 上传成功后：
        #
        # uploaded_at = 最近一次成功上传时间
        #
        # upload_result_json = 最近一次上传结果
        #
        # 不再代表“锁定”。
        # ============================================================

        if result.get(
            "uploaded"
        ):

            now = (
                datetime.now()
                .isoformat(
                    timespec="seconds"
                )
            )

            with db.connect() as conn:

                session = self._session(
                    conn,
                    project_id,
                    business_date,
                )

                conn.execute(
                    """
                    UPDATE review_sessions
                    SET
                        uploaded_at=?,
                        upload_result_json=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        now,
                        json.dumps(
                            result,
                            ensure_ascii=False,
                        ),
                        now,
                        session["id"],
                    ),
                )

            # 返回给前端的信息也明确：
            # 这次真的执行了上传。
            result["uploaded_at"] = now
            result["already_uploaded"] = False

        return result

review_service = ReviewService()
