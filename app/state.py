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
        self.db.execute("""CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS labels (key TEXT PRIMARY KEY, label TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS removals (
            key TEXT PRIMARY KEY, label TEXT, pairs TEXT, status TEXT, message_id TEXT, at REAL)""")
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

        Pending questions are never touched (the question stays open in Discord). A skipped title
        is only re-asked once REASK_DAYS have passed."""
        self.db.execute("DELETE FROM done WHERE key=?", (key,))
        self.db.execute("DELETE FROM skipped WHERE key=? AND at < ?",
                        (key, time.time() - skip_older_than_days * 86400))
        self.db.commit()

    # clean-up of titles deleted from Plex ---------------------------------
    def remember_labels(self, labels: dict[str, str]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO labels VALUES (?,?)", list(labels.items()))
        self.db.commit()

    def label_for(self, key: str) -> str:
        for q in ("SELECT label FROM labels WHERE key=?", "SELECT label FROM done WHERE key=?",
                  "SELECT label FROM skipped WHERE key=?"):
            row = self.db.execute(q, (key,)).fetchone()
            if row and row[0] and row[0] != key:
                return row[0]
        return ""

    def add_removal(self, key: str, label: str, pairs: list[tuple[str, str]]) -> None:
        self.db.execute("INSERT OR REPLACE INTO removals VALUES (?,?,?,?,?,?)",
                        (key, label, json.dumps(pairs), "pending", None, time.time()))
        self.db.commit()

    def get_removal(self, key: str) -> dict | None:
        row = self.db.execute("SELECT key,label,pairs,status,message_id FROM removals WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"key": row[0], "label": row[1], "pairs": [tuple(p) for p in json.loads(row[2])],
                "status": row[3], "message_id": row[4]}

    def removal_status(self, key: str) -> str:
        """'pending' or 'kept' blocks a new question; a 'removed' title still listed is asked again."""
        r = self.get_removal(key)
        return r["status"] if r and r["status"] in ("pending", "kept") else ""

    def set_removal_message(self, key: str, message_id: int) -> None:
        self.db.execute("UPDATE removals SET message_id=? WHERE key=?", (str(message_id), key))
        self.db.commit()

    def set_removal_status(self, key: str, status: str) -> None:
        self.db.execute("UPDATE removals SET status=? WHERE key=?", (status, key))
        self.db.commit()

    def set_removal_pairs(self, key: str, pairs: list[tuple[str, str]]) -> None:
        self.db.execute("UPDATE removals SET pairs=? WHERE key=?", (json.dumps(pairs), key))
        self.db.commit()

    def drop_removal(self, key: str) -> None:
        self.db.execute("DELETE FROM removals WHERE key=?", (key,))
        self.db.commit()

    def drop_removal_questions_except(self, still_missing: set[str]) -> list[tuple[str, str, str | None]]:
        """Open questions for titles that are back in Plex are void; returns (key, label, message_id)."""
        rows = self.db.execute("SELECT key,label,message_id FROM removals WHERE status='pending'").fetchall()
        back = [r for r in rows if r[0] not in still_missing]
        for r in back:
            self.db.execute("DELETE FROM removals WHERE key=?", (r[0],))
        self.db.commit()
        return back

    # profile ----------------------------------------------------------------
    def switch_profile(self, profile_id: str) -> int:
        """Record the NostalgiaTV profile in use. When it differs from the recorded one (including
        state written before profiles were tracked), placements recorded so far are forgotten so
        recent titles are re-checked against the right profile. Returns the rows cleared."""
        row = self.db.execute("SELECT value FROM meta WHERE name='profile'").fetchone()
        if row and row[0] == profile_id:
            return 0
        n = self.db.execute("DELETE FROM done").rowcount
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('profile', ?)", (profile_id,))
        self.db.commit()
        return n

    # counts for the status page ------------------------------------------------
    def count_pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    def count_removals_pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM removals WHERE status='pending'").fetchone()[0]

    def count_done(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM done").fetchone()[0]

    # recent placements for the status page -----------------------------------------
    def recent_placements(self, days: int = 14, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT key,label,channels,how,at FROM done WHERE how NOT LIKE 'already placed%' "
                               "AND how NOT LIKE 'dry run%' AND at >= ? ORDER BY at DESC LIMIT ?",
                               (time.time() - days * 86400, limit)).fetchall()
        return [{"key": r[0], "label": r[1], "channels": json.loads(r[2]), "how": r[3], "at": r[4]} for r in rows]

    def get_done(self, key: str) -> dict | None:
        r = self.db.execute("SELECT key,label,channels,how,at FROM done WHERE key=?", (key,)).fetchone()
        return {"key": r[0], "label": r[1], "channels": json.loads(r[2]), "how": r[3], "at": r[4]} if r else None

    def update_done(self, key: str, channels: list[str], how: str) -> None:
        self.db.execute("UPDATE done SET channels=?, how=? WHERE key=?", (json.dumps(channels), how, key))
        self.db.commit()
