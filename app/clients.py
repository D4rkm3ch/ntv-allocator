"""HTTP clients for Plex and the NostalgiaTV server."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .rules import Title

log = logging.getLogger("clients")


class Plex:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.h = {"X-Plex-Token": token, "Accept": "application/json"}
        self.http = httpx.AsyncClient(timeout=30, verify=False)
        self.machine_id: str | None = None
        self.sections: dict[str, dict[str, Any]] = {}

    async def init(self) -> None:
        r = await self.http.get(f"{self.url}/identity", headers=self.h)
        r.raise_for_status()
        self.machine_id = r.json()["MediaContainer"]["machineIdentifier"]
        r = await self.http.get(f"{self.url}/library/sections", headers=self.h)
        r.raise_for_status()
        self.sections = {d["key"]: d for d in r.json()["MediaContainer"].get("Directory", [])}
        log.info("Plex %s, %d libraries", self.machine_id, len(self.sections))

    async def recently_added(self, days: int, libraries_ignored: set[str]) -> list[str]:
        """Rating keys of shows and movies added (or with new episodes) in the last `days` days."""
        keys: list[str] = []
        for sid, sec in self.sections.items():
            if sec["title"] in libraries_ignored or sec["type"] not in ("show", "movie"):
                continue
            if sec["type"] == "movie":
                q = f"type=1&addedAt>>=-{days}d"
            else:
                q = f"type=2&addedAt>>=-{days}d"   # brand-new shows only; new episodes inherit the show's channels
            r = await self.http.get(f"{self.url}/library/sections/{sid}/all?{q}", headers=self.h)
            r.raise_for_status()
            keys += [m["ratingKey"] for m in r.json()["MediaContainer"].get("Metadata", [])]
        return keys

    async def title(self, key: str) -> Title | None:
        r = await self.http.get(f"{self.url}/library/metadata/{key}", headers=self.h)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        md = r.json()["MediaContainer"]["Metadata"][0]
        if md.get("type") not in ("show", "movie"):
            return None
        return Title(key=str(md["ratingKey"]), kind=md["type"], title=md.get("title", ""),
                     year=int(md.get("year") or 0), library=md.get("librarySectionTitle", ""),
                     studio=md.get("studio") or "", rating=md.get("contentRating") or "",
                     genres=[g["tag"] for g in md.get("Genre", [])], summary=md.get("summary") or "")

    def qualified(self, key: str) -> str:
        return f"{self.machine_id}:{key}"


class NostalgiaTV:
    def __init__(self, url: str, api_key: str = "", username: str = "", password: str = "", profile_id: str = ""):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.username, self.password = username, password
        self.params = {"profileId": profile_id} if profile_id else {}
        self.http = httpx.AsyncClient(timeout=60)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    async def init(self) -> None:
        r = await self.http.get(f"{self.url}/api/auth/status")
        r.raise_for_status()
        needs_login = bool(r.json().get("authRequired"))
        if needs_login and not self.api_key:
            if not (self.username and self.password):
                raise RuntimeError("NostalgiaTV requires login: set NTV_API_KEY or NTV_USERNAME/NTV_PASSWORD")
            lr = await self.http.post(f"{self.url}/api/auth/login",
                                      json={"username": self.username, "password": self.password})
            lr.raise_for_status()
            self.api_key = lr.json()["token"]
        log.info("NostalgiaTV reachable (login %s)", "required" if needs_login else "not configured")

    async def _req(self, method: str, path: str, **kw) -> Any:
        params = {**self.params, **kw.pop("params", {})}
        r = await self.http.request(method, f"{self.url}{path}", headers=self.headers, params=params, **kw)
        if r.status_code == 401 and self.username:
            self.api_key = ""
            await self.init()
            r = await self.http.request(method, f"{self.url}{path}", headers=self.headers, params=params, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    async def unassigned_keys(self) -> list[str]:
        """Keys the server says no visible channel schedules (format-tolerant)."""
        data = await self._req("GET", "/api/content/unassigned")
        items = data.get("items") or data.get("content") or data.get("unassigned") or [] if isinstance(data, dict) else data
        out = []
        for it in items or []:
            k = it.get("ratingKey") or it.get("key") or it.get("id") if isinstance(it, dict) else it
            if k:
                out.append(str(k).split(":")[-1])
        return out

    async def channels_containing(self, qualified_key: str) -> list[str]:
        data = await self._req("GET", "/api/content/channels-containing", params={"keys": qualified_key})
        chans = data.get("channels", data) if isinstance(data, dict) else data
        out = []
        for c in chans or []:
            out.append(c.get("stableId") or c.get("id") or c.get("channelId") if isinstance(c, dict) else str(c))
        return [c for c in out if c]

    async def add_to_channel(self, channel_id: str, qualified_keys: list[str]) -> dict:
        return await self._req("POST", "/api/content/add-to-channel",
                               json={"channelId": channel_id, "ratingKeys": qualified_keys})

    async def backup(self) -> None:
        await self._req("POST", "/api/config/backups/create")

    async def refresh_channel(self, channel_id: str) -> None:
        await self._req("POST", f"/api/schedule/refresh-channel/{channel_id}")
