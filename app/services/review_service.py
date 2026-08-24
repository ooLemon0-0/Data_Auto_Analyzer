from __future__ import annotations

import json
import random
from datetime import date, datetime
from typing import Any

from app import db
from app.core.config import settings
from app.core.registry import build_sink, build_source


class ReviewService:
    def project_dict(self, project_id: str) -> dict[str, Any]:
        return settings.project(project_id).model_dump()

    @staticmethod
    def _stable_order(ids: list[int], project_id: str, business_date: str) -> list[int]:
        ordered = list(ids)
        random.Random(f"{project_id}:{business_date}").shuffle(ordered)
        return ordered

    def prepare(self, project_id: str, business_date: date, target_size: int | None = None) -> dict[str, Any]:
        project = self.project_dict(project_id)
        target = int(target_size or project["daily_target"])
        if target < 1:
            raise ValueError("target_size must be >= 1")

        # Re-opening an existing day should be instant. Fetch from the intranet only if the day
        # has not been indexed yet, or config explicitly requests refresh.
        with db.connect() as conn:
            existing_items = conn.execute(
                "SELECT COUNT(*) FROM items WHERE project_id=? AND business_date=?",
                (project_id, business_date.isoformat()),
            ).fetchone()[0]
        refresh = bool(project.get("source", {}).get("refresh_on_prepare", False))
        if existing_items == 0 or existing_items < target or refresh:
            source = build_source(project)
            items = source.fetch_day(business_date)
            db.upsert_items(project_id, business_date.isoformat(), items)

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

        return self.state(project_id, business_date)

    def _session(self, conn, project_id: str, business_date: date):
        session = conn.execute(
            "SELECT * FROM review_sessions WHERE project_id=? AND business_date=?",
            (project_id, business_date.isoformat()),
        ).fetchone()
        if not session:
            raise RuntimeError("Review session has not been prepared yet")
        return session

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
        if decision not in {"correct", "incorrect", "invalid"}:
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
