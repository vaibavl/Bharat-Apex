"""SQLite persistence for login sessions and chat history."""

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.Lock()


def _db_path() -> str:
    path = os.getenv("DATABASE_PATH", os.path.join("data", "clientcare.db"))
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_ROOT, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


@contextmanager
def connect():
    conn = sqlite3.connect(_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _lock, connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash  TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                username    TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id          TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                title       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_customer ON conversations(customer_id, updated_at);
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                meta            TEXT,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
            """
        )


# ----------------------------------------------------------- sessions
def save_session(token_hash: str, customer_id: str, username: str, expires_at: str) -> None:
    with _lock, connect() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        c.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
            (token_hash, customer_id, username, expires_at),
        )


def load_session(token_hash: str) -> Optional[Dict[str, Any]]:
    with connect() as c:
        row = c.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
    if not row or row["expires_at"] < now_iso():
        return None
    return dict(row)


def delete_session(token_hash: str) -> None:
    with _lock, connect() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


# ------------------------------------------------------ conversations
def create_conversation(customer_id: str, title: str = "New chat") -> Dict[str, Any]:
    cid, ts = uuid.uuid4().hex, now_iso()
    with _lock, connect() as c:
        c.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", (cid, customer_id, title, ts, ts))
    return {"id": cid, "title": title, "created_at": ts, "updated_at": ts}


def list_conversations(customer_id: str) -> List[Dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE customer_id = ? ORDER BY updated_at DESC",
            (customer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(customer_id: str, conv_id: str) -> Optional[Dict[str, Any]]:
    """Returns the conversation only if it belongs to this customer."""
    with connect() as c:
        row = c.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND customer_id = ?",
            (conv_id, customer_id),
        ).fetchone()
    return dict(row) if row else None


def rename_conversation(customer_id: str, conv_id: str, title: str) -> bool:
    with _lock, connect() as c:
        cur = c.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND customer_id = ?",
            (title, conv_id, customer_id),
        )
        return cur.rowcount > 0


def delete_conversation(customer_id: str, conv_id: str) -> bool:
    with _lock, connect() as c:
        cur = c.execute(
            "DELETE FROM conversations WHERE id = ? AND customer_id = ?", (conv_id, customer_id)
        )
        return cur.rowcount > 0


# ----------------------------------------------------------- messages
def add_message(conv_id: str, role: str, content: str, meta: Optional[str] = None) -> None:
    ts = now_iso()
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (conv_id, role, content, meta, ts),
        )
        c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conv_id))


def get_messages(conv_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    with connect() as c:
        if limit:
            rows = c.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (conv_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC", (conv_id,)
            ).fetchall()
    return [dict(r) for r in rows]
