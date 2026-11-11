"""SQLite state: pending questions, decisions and an audit log."""
from __future__ import annotations

import json
import sqlite3
import time


class State:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS pending (
            key TEXT PRIMARY KEY, label TEXT, suggested TEXT, selected TEXT,
            message_id TEXT, created REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS done (
            key TEXT PRIMARY KEY, label TEXT, channels TEXT, how TEXT, at REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS skipped (key TEXT PRIMARY KEY, label TEXT, at REAL)""")
        self.db.commit()

    # pending ---------------------------------------------------------------
    def is_known(self, key: str) -> bool:
        q = "SELECT 1 FROM pending WHERE key=? UNION SELECT 1 FROM done WHERE key=? UNION SELECT 1 FROM skipped WHERE key=?"
        return self.db.execute(q, (key, key, key)).fetchone() is not None

    def add_pending(self, key: str, label: str, suggested: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?,?)",
                        (key, label, json.dumps(suggested), json.dumps(suggested), None, time.time()))
        self.db.commit()

    def set_message(self, key: str, message_id: int) -> None:
        self.db.execute("UPDATE pending SET message_id=? WHERE key=?", (str(message_id), key))
        self.db.commit()

    def set_selected(self, key: str, selected: list[str]) -> None:
        self.db.execute("UPDATE pending SET selected=? WHERE key=?", (json.dumps(selected), key))
        self.db.commit()

    def get_pending(self, key: str) -> dict | None:
        row = self.db.execute("SELECT key,label,suggested,selected,message_id FROM pending WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"key": row[0], "label": row[1], "suggested": json.loads(row[2]), "selected": json.loads(row[3]),
                "message_id": row[4]}

    def unsent(self) -> list[dict]:
        rows = self.db.execute("SELECT key FROM pending WHERE message_id IS NULL").fetchall()
        return [self.get_pending(r[0]) for r in rows]

    # resolution --------------------------------------------------------------
    def mark_done(self, key: str, label: str, channels: list[str], how: str) -> None:
        self.db.execute("DELETE FROM pending WHERE key=?", (key,))
        self.db.execute("INSERT OR REPLACE INTO done VALUES (?,?,?,?,?)", (key, label, json.dumps(channels), how, time.time()))
        self.db.commit()

    def mark_skipped(self, key: str, label: str) -> None:
        self.db.execute("DELETE FROM pending WHERE key=?", (key,))
        self.db.execute("INSERT OR REPLACE INTO skipped VALUES (?,?,?)", (key, label, time.time()))
        self.db.commit()

    def clear_dry_run(self) -> int:
        cur = self.db.execute("DELETE FROM done WHERE how LIKE 'dry run%'")
        self.db.commit()
        return cur.rowcount

    def forget_resolved(self, key: str, skip_older_than_days: int) -> None:
        """Let a title the server reports unassigned be re-evaluated.

        Pending questions are never touched (the question stays open in Discord). A title left on
        New Arrivals is only re-asked once the New Arrivals window has passed."""
        self.db.execute("DELETE FROM done WHERE key=?", (key,))
        self.db.execute("DELETE FROM skipped WHERE key=? AND at < ?",
                        (key, time.time() - skip_older_than_days * 86400))
        self.db.commit()
