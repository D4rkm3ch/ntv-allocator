"""Admin password, first-time setup code and login sessions for the status page.

- The password is stored as a salted scrypt hash; it is never logged or shown.
- Until a password exists, a one-time setup code is printed in the container log. Creating the
  password needs that code, so only someone who can read the NAS's logs can claim the instance.
- Sessions are random tokens in an HttpOnly, SameSite=Strict cookie; only their hashes are stored.
- Repeated wrong passwords lock login for a while.
- `python -m app.reset_password` clears the password and sessions and prints a new setup code.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

SESSION_DAYS = 30
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
MIN_PASSWORD = 8


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        cand = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(cand.hex(), h)
    except (ValueError, AttributeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_setup_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # no easily confused characters
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


class Auth:
    def __init__(self, state):
        self.state = state
        self.fails: dict[str, list[float]] = {}

    # setup -------------------------------------------------------------------
    def is_set_up(self) -> bool:
        return bool(self.state.get_secret("password"))

    def setup_code(self) -> str:
        """Current setup code, creating one if needed (only meaningful before setup)."""
        code = self.state.get_secret("setup_code")
        if not code:
            code = new_setup_code()
            self.state.set_secret("setup_code", code)
        return code

    def set_up(self, code: str, pw: str, confirm: str) -> str | None:
        """Create the admin password. Returns an error message, or None on success."""
        if self.is_set_up():
            return "A password already exists."
        expected = self.state.get_secret("setup_code") or ""
        if not expected or not hmac.compare_digest(code.strip().upper(), expected):
            return "The setup code is not correct. It is printed in the container log."
        err = self._password_problem(pw, confirm)
        if err:
            return err
        self.state.set_secret("password", hash_password(pw))
        self.state.set_secret("setup_code", "")
        return None

    def _password_problem(self, pw: str, confirm: str) -> str | None:
        if len(pw) < MIN_PASSWORD:
            return f"Use at least {MIN_PASSWORD} characters."
        if pw != confirm:
            return "The two passwords do not match."
        return None

    def change_password(self, current: str, pw: str, confirm: str) -> str | None:
        if not check_password(current, self.state.get_secret("password") or ""):
            return "The current password is not correct."
        err = self._password_problem(pw, confirm)
        if err:
            return err
        self.state.set_secret("password", hash_password(pw))
        self.state.clear_sessions()                         # sign out everywhere else
        return None

    # login -------------------------------------------------------------------
    def locked(self, ip: str) -> int:
        now = time.time()
        recent = [t for t in self.fails.get(ip, []) if now - t < LOCK_SECONDS]
        self.fails[ip] = recent
        if len(recent) >= MAX_FAILS:
            return int(LOCK_SECONDS - (now - recent[0])) + 1
        return 0

    def login(self, ip: str, pw: str) -> str | None:
        """Returns a new session token, or None for a wrong password."""
        if check_password(pw, self.state.get_secret("password") or ""):
            self.fails.pop(ip, None)
            token = secrets.token_urlsafe(32)
            self.state.add_session(_token_hash(token), time.time() + SESSION_DAYS * 86400)
            return token
        self.fails.setdefault(ip, []).append(time.time())
        return None

    def valid(self, token: str | None) -> bool:
        return bool(token) and self.state.session_valid(_token_hash(token))

    def logout(self, token: str | None) -> None:
        if token:
            self.state.drop_session(_token_hash(token))

    def reset(self) -> str:
        """Forgotten password: clear it and all sessions, return a fresh setup code."""
        self.state.set_secret("password", "")
        self.state.clear_sessions()
        code = new_setup_code()
        self.state.set_secret("setup_code", code)
        return code
