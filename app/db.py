from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.core.config import settings
from app.core.models import SourceItem

DB_PATH = Path(settings.storage.db_path).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                source_key TEXT NOT NULL,
                image_path TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                recognition_text TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(project_id, business_date, source_key)
            );

            CREATE TABLE IF NOT EXISTS review_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                target_size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                uploaded_at TEXT,
                upload_result_json TEXT,
                UNIQUE(project_id, business_date)
            );

            CREATE TABLE IF NOT EXISTS review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                decision TEXT,
                reviewed_at TEXT,
                UNIQUE(session_id, item_id),
                UNIQUE(session_id, seq)
            );
            """
        )
        # Migrate databases created by v0.1 without deleting existing audit data.
        _ensure_column(conn, "items", "image_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "review_sessions", "uploaded_at", "TEXT")
        _ensure_column(conn, "review_sessions", "upload_result_json", "TEXT")


def upsert_items(project_id: str, business_date: str, items: Iterable[SourceItem]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    with connect() as conn:
        for item in items:
            image_path = str(item.image_path.resolve()) if item.image_path else ""
            conn.execute(
                """
                INSERT INTO items(project_id, business_date, source_key, image_path, image_url,
                                  recognition_text, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, business_date, source_key) DO UPDATE SET
                    image_path=excluded.image_path,
                    image_url=excluded.image_url,
                    recognition_text=excluded.recognition_text,
                    metadata_json=excluded.metadata_json
                """,
                (
                    project_id,
                    business_date,
                    item.source_key,
                    image_path,
                    item.image_url or "",
                    item.recognition_text,
                    json.dumps(item.metadata, ensure_ascii=False),
                    now,
                ),
            )
            count += 1
    return count
