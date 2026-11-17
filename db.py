"""SQLite storage.

- users:  Google account + encrypted OAuth token
- events: one JSON document per event, written only by the background worker
          (plus once at creation)
- inbox:  everything that happens *to* an event (a guest replied, the organizer
          clicked Book, the organizer sent the agent a note). Web requests only
          append here; the worker applies them. That way a reply arriving while the
          agent is mid-run can never be overwritten.
- tokens: reply token -> event, for routing inbound email
- chat:   the conversation shown on the event page. Append-only (plus marking a
          widget answered), so the web app and the worker can both add to it
          without stepping on the event document.
- shopping: the shopping list, one JSON document per event. Written by the list
          builder and by the organizer's edits, never by the agent.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from cryptography.fernet import Fernet

import config
from models import Event, now_utc

_fernet = Fernet(config.TOKEN_ENCRYPTION_KEY.encode())
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT, token_enc BLOB, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS events_user ON events(user_id);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    dedupe_key TEXT UNIQUE, created_at TEXT, processed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(event_id, processed);
CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, event_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    widget TEXT, answered INTEGER DEFAULT 0, created_at TEXT
);
CREATE INDEX IF NOT EXISTS chat_event ON chat(event_id, id);
CREATE TABLE IF NOT EXISTS shopping (event_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


# ---------------- users ----------------


def save_user(user_id: str, email: str, name: str, token_json: str) -> None:
    with _write_lock, connect() as c:
        c.execute(
            "INSERT INTO users (id, email, name, token_enc, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET email=excluded.email, name=excluded.name, token_enc=excluded.token_enc",
            (user_id, email, name, _fernet.encrypt(token_json.encode()), now_utc().isoformat()),
        )


def get_user(user_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT id, email, name FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_token(user_id: str) -> str | None:
    with connect() as c:
        row = c.execute("SELECT token_enc FROM users WHERE id=?", (user_id,)).fetchone()
    return _fernet.decrypt(row["token_enc"]).decode() if row and row["token_enc"] else None


def delete_user_and_data(user_id: str) -> None:
    """Remove a user, their events, and everything attached to them."""
    with _write_lock, connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM events WHERE user_id=?", (user_id,))]
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
    delete_events(ids)


def delete_events(event_ids: list[str]) -> None:
    with _write_lock, connect() as c:
        for table in ("inbox", "tokens", "chat", "shopping", "events"):
            col = "id" if table == "events" else "event_id"
            c.executemany(f"DELETE FROM {table} WHERE {col}=?", [(i,) for i in event_ids])


# ---------------- events ----------------


def create_event(event: Event) -> None:
    with _write_lock, connect() as c:
        c.execute(
            "INSERT INTO events (id, user_id, data, updated_at) VALUES (?,?,?,?)",
            (event.id, event.user_id, json.dumps(event.to_dict()), now_utc().isoformat()),
        )
        _save_tokens(c, event)


def save_event(event: Event) -> None:
    """Update an existing event. A no-op if it was deleted (e.g. by Reset demo mid-run)."""
    with _write_lock, connect() as c:
        cur = c.execute(
            "UPDATE events SET data=?, user_id=?, updated_at=? WHERE id=?",
            (json.dumps(event.to_dict()), event.user_id, now_utc().isoformat(), event.id),
        )
        if cur.rowcount:
            _save_tokens(c, event)


def _save_tokens(c, event: Event) -> None:
    c.executemany("INSERT OR IGNORE INTO tokens (token, event_id) VALUES (?,?)",
                  [(g.token, event.id) for g in event.guests])


def load_event(event_id: str) -> Event | None:
    with connect() as c:
        row = c.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
    return Event.from_dict(json.loads(row["data"])) if row else None


def list_events(user_id: str | None = None) -> list[Event]:
    with connect() as c:
        if user_id:
            rows = c.execute(
                "SELECT data FROM events WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
            ).fetchall()
        else:
            rows = c.execute("SELECT data FROM events").fetchall()
    return [Event.from_dict(json.loads(r["data"])) for r in rows]


def event_id_for_token(token: str) -> str | None:
    with connect() as c:
        row = c.execute("SELECT event_id FROM tokens WHERE token=?", (token,)).fetchone()
    return row["event_id"] if row else None


# ---------------- inbox ----------------


def add_inbox(event_id: str, kind: str, payload: dict, dedupe_key: str | None = None) -> bool:
    """Returns False if an item with this dedupe_key was already received."""
    with _write_lock, connect() as c:
        try:
            c.execute(
                "INSERT INTO inbox (event_id, kind, payload, dedupe_key, created_at) VALUES (?,?,?,?,?)",
                (event_id, kind, json.dumps(payload), dedupe_key, now_utc().isoformat()),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def pending_inbox(event_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, kind, payload FROM inbox WHERE event_id=? AND processed=0 ORDER BY id", (event_id,)
        ).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"])} for r in rows]


def mark_processed(ids: list[int]) -> None:
    if not ids:
        return
    with _write_lock, connect() as c:
        c.executemany("UPDATE inbox SET processed=1 WHERE id=?", [(i,) for i in ids])


# ---------------- chat ----------------


def add_chat(event_id: str, role: str, text: str, kind: str = "text", widget: dict | None = None) -> int:
    """role: agent | user. kind: text | email | notice. A new widget closes older open
    widgets of the same type, so only the latest one can be answered."""
    with _write_lock, connect() as c:
        if widget:
            rows = c.execute("SELECT id, widget FROM chat WHERE event_id=? AND answered=0 AND widget IS NOT NULL",
                             (event_id,)).fetchall()
            stale = [r["id"] for r in rows if json.loads(r["widget"]).get("type") == widget.get("type")]
            c.executemany("UPDATE chat SET answered=1 WHERE id=?", [(i,) for i in stale])
        cur = c.execute(
            "INSERT INTO chat (event_id, role, kind, text, widget, created_at) VALUES (?,?,?,?,?,?)",
            (event_id, role, kind, text, json.dumps(widget) if widget else None, now_utc().isoformat()),
        )
        return cur.lastrowid


def update_chat_text(message_id: int, text: str, widget: dict | None = None) -> None:
    with _write_lock, connect() as c:
        c.execute("UPDATE chat SET text=?, widget=COALESCE(?, widget), created_at=? WHERE id=?",
                  (text, json.dumps(widget) if widget else None, now_utc().isoformat(), message_id))


def list_chat(event_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM chat WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
    return [{**dict(r), "widget": json.loads(r["widget"]) if r["widget"] else None, "answered": bool(r["answered"])}
            for r in rows]


def get_chat(message_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM chat WHERE id=?", (message_id,)).fetchone()
    return {**dict(r), "widget": json.loads(r["widget"]) if r["widget"] else None, "answered": bool(r["answered"])} if r else None


def answer_widget(message_id: int) -> bool:
    """Mark a widget answered. False if it already was (a double click, or superseded)."""
    with _write_lock, connect() as c:
        cur = c.execute("UPDATE chat SET answered=1 WHERE id=? AND answered=0", (message_id,))
        return cur.rowcount == 1


def count_user_messages(event_id: str) -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM chat WHERE event_id=? AND role='user' AND kind='typed'",
                         (event_id,)).fetchone()[0]


# ---------------- shopping list ----------------


def get_shopping(event_id: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT data FROM shopping WHERE event_id=?", (event_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def update_shopping(event_id: str, change) -> dict | None:
    """Read-modify-write under the lock. `change(data or None)` returns the new data (or None
    to leave it unchanged)."""
    with _write_lock, connect() as c:
        row = c.execute("SELECT data FROM shopping WHERE event_id=?", (event_id,)).fetchone()
        new = change(json.loads(row["data"]) if row else None)
        if new is not None:
            c.execute(
                "INSERT INTO shopping (event_id, data, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (event_id, json.dumps(new), now_utc().isoformat()),
            )
        return new


def close_open_widgets(event_id: str) -> None:
    with _write_lock, connect() as c:
        c.execute("UPDATE chat SET answered=1 WHERE event_id=? AND answered=0 AND widget IS NOT NULL "
                  "AND kind='text'", (event_id,))
