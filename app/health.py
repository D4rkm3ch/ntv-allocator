"""Connection health: decides when to tell Discord that something stopped (or started) working.

One alert when a service fails `threshold` checks in a row, a reminder every `remind_hours` while it
stays down, and one recovery message. Messages wait in `outbox` until they are actually sent, so an
alert raised while Discord itself is unreachable is delivered once it is back.
"""
from __future__ import annotations

import time

import httpx


def describe(exc: BaseException, url: str = "") -> str:
    """A short, readable reason for a failure."""
    where = f" at {url}" if url else ""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"rejected the token or login (HTTP {code})"
        return f"answered with an error (HTTP {code})"
    if isinstance(exc, httpx.TimeoutException):
        return f"did not answer in time{where}"
    if isinstance(exc, httpx.ConnectError):
        return f"is not reachable{where}"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


class Health:
    def __init__(self, threshold: int = 3, remind_hours: float = 6, clock=time.time):
        self.threshold = threshold
        self.remind = remind_hours * 3600
        self.clock = clock
        self.s: dict[str, dict] = {}
        self.outbox: list[str] = []

    def is_down(self, name: str) -> bool:
        return bool(self.s.get(name, {}).get("fails"))

    def ok(self, name: str) -> None:
        st = self.s.get(name)
        if not st:
            return
        if st.get("alerted"):
            self.outbox.append(f"**{name} is working again** (was down for "
                               f"{duration(self.clock() - st['since'])}).")
        self.s.pop(name, None)

    def fail(self, name: str, reason: str, threshold: int | None = None) -> None:
        now = self.clock()
        st = self.s.setdefault(name, {"fails": 0, "since": now, "alerted": False, "reminded": now})
        st["fails"] += 1
        st["reason"] = reason
        if not st["alerted"] and st["fails"] >= (threshold or self.threshold):
            st["alerted"], st["reminded"] = True, now
            self.outbox.append(f"**{name} problem**: {reason}. The allocator keeps retrying and will post "
                               f"here when it works again.")
        elif st["alerted"] and now - st["reminded"] >= self.remind:
            st["reminded"] = now
            self.outbox.append(f"**{name} still not working** (down for {duration(now - st['since'])}): {reason}.")
