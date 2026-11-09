from __future__ import annotations

import hashlib
import json
import time

from app import db
from app.services.review_service import ReviewService


def fingerprint(state: dict) -> str:
    values = [state[key] for key in ("project_id", "business_date", "target_size", "correct", "incorrect", "invalid")]
    values.append(state.get('other', 0))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


class DesktopSync:
    """Durable upload queue. Review changes survive login/network failures."""

    def __init__(self, service: ReviewService | None = None):
        self.service = service or ReviewService()
        with db.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS desktop_sync_jobs (
                project_id TEXT NOT NULL, business_date TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(project_id,business_date))""")
            # A process interrupted during an upload can safely upsert the date again.
            conn.execute("UPDATE desktop_sync_jobs SET status='pending' WHERE status='running'")

    def enqueue(self, state: dict) -> bool:
        if not state["complete"]:
            with db.connect() as conn:
                conn.execute("DELETE FROM desktop_sync_jobs WHERE project_id=? AND business_date=?",
                             (state["project_id"], state["business_date"]))
            return False
        digest = fingerprint(state)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM desktop_sync_jobs WHERE project_id=? AND business_date=?",
                               (state["project_id"], state["business_date"])).fetchone()
            if row and row["fingerprint"] == digest:
                return False
            conn.execute("""INSERT INTO desktop_sync_jobs
                (project_id,business_date,fingerprint,status) VALUES (?,?,?,'pending')
                ON CONFLICT(project_id,business_date) DO UPDATE SET
                fingerprint=excluded.fingerprint,status='pending',attempts=0,next_retry=0,last_error=''""",
                         (state["project_id"], state["business_date"], digest))
        return True

    def due(self) -> dict | None:
        with db.connect() as conn:
            row = conn.execute("""SELECT * FROM desktop_sync_jobs
                WHERE status IN ('pending','retry') AND next_retry<=?
                ORDER BY next_retry,business_date LIMIT 1""", (time.time(),)).fetchone()
        return dict(row) if row else None

    def retry(self, project_id: str, business_date: str) -> None:
        with db.connect() as conn:
            conn.execute("""UPDATE desktop_sync_jobs SET status='pending',next_retry=-1,attempts=0
                WHERE project_id=? AND business_date=?""", (project_id, business_date))

    def run(self, job: dict) -> dict:
        from datetime import date
        project_id, day = job["project_id"], date.fromisoformat(job["business_date"])
        state = self.service.state(project_id, day)
        if not state["complete"] or fingerprint(state) != job["fingerprint"]:
            self.enqueue(state)
            return {"uploaded": False, "stale": True}
        with db.connect() as conn:
            conn.execute("UPDATE desktop_sync_jobs SET status='running' WHERE project_id=? AND business_date=?",
                         (project_id, day.isoformat()))
        try:
            result = self.service.upload(project_id, day)
            if not result.get("uploaded"):
                raise RuntimeError("上传未完成，统计已保存在本机")
        except Exception as exc:
            message = str(exc)
            attempts = job["attempts"] + 1
            needs_auth = any(word in message for word in ("登录", "扫码", "手机验证", "验证超时"))
            status = "auth_required" if needs_auth else "error" if attempts >= 5 else "retry"
            with db.connect() as conn:
                conn.execute("""UPDATE desktop_sync_jobs SET status=?,attempts=?,next_retry=?,last_error=?
                    WHERE project_id=? AND business_date=?""",
                             (status, attempts, time.time() + min(30 * 2 ** (attempts - 1), 300),
                              message, project_id, day.isoformat()))
            raise
        with db.connect() as conn:
            conn.execute("""UPDATE desktop_sync_jobs SET status='succeeded',last_error='',next_retry=0
                WHERE project_id=? AND business_date=?""", (project_id, day.isoformat()))
        return result
