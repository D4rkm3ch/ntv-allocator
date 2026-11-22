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
        self.db.execute("""CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS labels (key TEXT PRIMARY KEY, label TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS removals (
            key TEXT PRIMARY KEY, label TEXT, pairs TEXT, status TEXT, message_id TEXT, at REAL)""")
        # seasonal: one row per (title, season) once decided: added, present, asked, declined, undone, dry run
        self.db.execute("""CREATE TABLE IF NOT EXISTS seasonal (
            key TEXT, season TEXT, label TEXT, sid TEXT, status TEXT, collection TEXT, batch TEXT, at REAL,
            PRIMARY KEY (key, season))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS seasonal_batches (
            batch TEXT PRIMARY KEY, kind TEXT, season TEXT, keys TEXT, selected TEXT)""")
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

    def count_placed_since(self, how_like: str, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM done WHERE how LIKE ? AND at >= ?", (how_like, since)).fetchone()[0]

    def all_pending(self) -> list[dict]:
        return [self.get_pending(r[0]) for r in self.db.execute("SELECT key FROM pending ORDER BY created")]

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
        n = cur.rowcount
        n += self.db.execute("DELETE FROM seasonal WHERE status='dry run'").rowcount
        self.db.commit()
        return n

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

    def done_keys(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT key FROM done").fetchall()]

    def drop_done(self, key: str) -> None:
        self.db.execute("DELETE FROM done WHERE key=?", (key,))
        self.db.commit()

    # settings, admin password and sessions --------------------------------------------
    def get_settings(self) -> dict:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT name, value FROM settings").fetchall()}

    def set_setting(self, name: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (name, json.dumps(value)))
        self.db.commit()

    def get_secret(self, name: str) -> str:
        row = self.db.execute("SELECT value FROM secrets WHERE name=?", (name,)).fetchone()
        return row[0] if row else ""

    def set_secret(self, name: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO secrets VALUES (?,?)", (name, value))
        self.db.commit()

    def add_session(self, token_hash: str, expires: float) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (token_hash, expires))
        self.db.commit()

    def session_valid(self, token_hash: str) -> bool:
        row = self.db.execute("SELECT expires FROM sessions WHERE token=?", (token_hash,)).fetchone()
        return bool(row) and row[0] > time.time()

    def drop_session(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token=?", (token_hash,))
        self.db.commit()

    def clear_sessions(self) -> None:
        self.db.execute("DELETE FROM sessions")
        self.db.commit()

    # seasonal ------------------------------------------------------------------
    def seasonal_known(self, season: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT key FROM seasonal WHERE season=?", (season,))}

    def seasonal_set(self, key: str, season: str, label: str, sid: str, status: str,
                     collection: str = "", batch: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO seasonal VALUES (?,?,?,?,?,?,?,?)",
                        (key, season, label, sid, status, collection, batch, time.time()))
        self.db.commit()

    def seasonal_get(self, key: str, season: str) -> dict | None:
        r = self.db.execute("SELECT key,season,label,sid,status,collection,batch FROM seasonal WHERE key=? AND season=?",
                            (key, season)).fetchone()
        return dict(zip(("key", "season", "label", "sid", "status", "collection", "batch"), r)) if r else None

    def seasonal_batch_add(self, batch: str, kind: str, season: str, keys: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO seasonal_batches VALUES (?,?,?,?,?)",
                        (batch, kind, season, json.dumps(keys), "[]"))
        self.db.commit()

    def seasonal_batch(self, batch: str) -> dict | None:
        r = self.db.execute("SELECT batch,kind,season,keys,selected FROM seasonal_batches WHERE batch=?", (batch,)).fetchone()
        if not r:
            return None
        return {"batch": r[0], "kind": r[1], "season": r[2], "keys": json.loads(r[3]), "selected": json.loads(r[4])}

    def seasonal_batch_update(self, batch: str, keys: list[str] | None = None, selected: list[str] | None = None) -> None:
        if keys is not None:
            self.db.execute("UPDATE seasonal_batches SET keys=? WHERE batch=?", (json.dumps(keys), batch))
        if selected is not None:
            self.db.execute("UPDATE seasonal_batches SET selected=? WHERE batch=?", (json.dumps(selected), batch))
        self.db.commit()
