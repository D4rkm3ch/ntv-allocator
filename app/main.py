"""NostalgiaTV Channel Allocator.

Finds shows and movies that have no permanent channel in NostalgiaTV, places them using
rules.yaml, and asks in Discord when a placement is not certain.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict

import discord

from .clients import NostalgiaTV, Plex
from .rules import Rules, Title
from .state import State

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("allocator")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


CFG = {
    "plex_url": env("PLEX_URL"), "plex_token": env("PLEX_TOKEN"),
    "ntv_url": env("NTV_URL", "http://nostalgiatv:19850"), "ntv_key": env("NTV_API_KEY"),
    "ntv_user": env("NTV_USERNAME"), "ntv_pass": env("NTV_PASSWORD"), "ntv_profile": env("NTV_PROFILE_ID"),
    "discord_token": env("DISCORD_TOKEN"), "discord_channel": int(env("DISCORD_CHANNEL_ID", "0") or 0),
    "allowed_users": {int(x) for x in env("DISCORD_ALLOWED_USER_IDS").split(",") if x.strip()},
    "interval": int(env("SCAN_INTERVAL_MINUTES", "15")), "lookback": int(env("LOOKBACK_DAYS", "35")),
    "dry_run": env("DRY_RUN", "false").lower() in ("1", "true", "yes"),
    "rules": env("RULES_PATH", "/config/rules.yaml"), "state": env("STATE_PATH", "/data/state.db"),
}


class Allocator(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.rules = Rules(CFG["rules"])
        self.state = State(CFG["state"])
        self.plex = Plex(CFG["plex_url"], CFG["plex_token"])
        self.ntv = NostalgiaTV(CFG["ntv_url"], CFG["ntv_key"], CFG["ntv_user"], CFG["ntv_pass"], CFG["ntv_profile"])
        self.lock = asyncio.Lock()
        self.titles: dict[str, Title] = {}

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self._connect("Plex", CFG["plex_url"], self.plex.init)
        await self._connect("NostalgiaTV", CFG["ntv_url"], self.ntv.init)
        self.loop.create_task(self._scan_loop())

    @staticmethod
    async def _connect(name: str, url: str, init) -> None:
        """Retry until the server answers, with a readable log line instead of a crash loop."""
        delay = 10
        while True:
            try:
                await init()
                return
            except Exception as exc:
                log.error("cannot reach %s at %s (%s: %s); retrying in %ds",
                          name, url, type(exc).__name__, exc or "no response", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def on_ready(self) -> None:
        log.info("Discord connected as %s", self.user)

    async def _channel(self) -> discord.abc.Messageable:
        ch = self.get_channel(CFG["discord_channel"]) or await self.fetch_channel(CFG["discord_channel"])
        return ch

    async def _scan_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.scan()
            except Exception:
                log.exception("scan failed")
                try:
                    await (await self._channel()).send("Allocator scan failed. See container logs for details.")
                except Exception:
                    log.exception("could not report failure to Discord")
            await asyncio.sleep(CFG["interval"] * 60)

    # ------------------------------------------------------------------ scanning
    async def scan(self) -> None:
        async with self.lock:
            unassigned = set(await self.ntv.unassigned_keys())
            recent = set(await self.plex.recently_added(CFG["lookback"], self.rules.ignore_libraries))
            # a title the server reports unassigned is re-evaluated even if handled before
            for k in unassigned:
                self.state.forget(k)
            candidates = [k for k in unassigned | recent if not self.state.is_known(k)]
            log.info("scan: %d unassigned, %d recent, %d to evaluate", len(unassigned), len(recent), len(candidates))

            auto: dict[str, list[str]] = {}
            for key in candidates:
                t = await self.plex.title(key)
                if t is None or t.library in self.rules.ignore_libraries:
                    self.state.mark_skipped(key, t.label if t else key)   # not ours; do not look it up again
                    continue
                self.titles[key] = t
                homes = [c for c in await self.ntv.channels_containing(self.plex.qualified(key))
                         if c not in self.rules.not_a_home]
                if homes:
                    self.state.mark_done(key, t.label, homes, "already placed")
                    continue
                p = self.rules.place(t)
                if p.auto and p.channels:
                    auto[key] = p.channels
                else:
                    self.state.add_pending(key, t.label, p.channels)

            if auto:
                await self.apply(auto, how="auto")
                await self._post_summary(auto)
            for pend in self.state.unsent():
                await self._post_question(pend)

    async def apply(self, placements: dict[str, list[str]], how: str) -> None:
        by_channel: dict[str, list[str]] = defaultdict(list)
        for key, chans in placements.items():
            for c in chans:
                by_channel[c].append(self.plex.qualified(key))
        if CFG["dry_run"]:
            log.info("DRY RUN, would add: %s", dict(by_channel))
        else:
            await self.ntv.backup()
            for ch, keys in by_channel.items():
                res = await self.ntv.add_to_channel(ch, keys)
                refused = (res or {}).get("refused") or []
                if refused:
                    log.warning("channel %s refused %s", ch, refused)
                await self.ntv.refresh_channel(ch)
        for key, chans in placements.items():
            label = self.titles[key].label if key in self.titles else key
            self.state.mark_done(key, label, chans, how)

    # ------------------------------------------------------------------ Discord output
    def _names(self, chans: list[str]) -> str:
        return ", ".join(self.rules.names.get(c, c) for c in chans) or "none"

    async def _post_summary(self, placed: dict[str, list[str]]) -> None:
        lines = [f"{self.titles[k].label} [{self.titles[k].library}]: {self._names(c)}" for k, c in placed.items()]
        head = "Placed automatically" + (" (dry run, nothing changed)" if CFG["dry_run"] else "")
        ch = await self._channel()
        for i in range(0, len(lines), 20):
            await ch.send(f"**{head}**\n" + "\n".join(lines[i:i + 20]))

    async def _post_question(self, pend: dict) -> None:
        key = pend["key"]
        t = self.titles.get(key) or await self.plex.title(key)
        if t is None:
            self.state.mark_skipped(key, pend["label"])
            return
        self.titles[key] = t
        embed = discord.Embed(title=t.label, description=(t.summary[:300] + "...") if len(t.summary) > 300 else t.summary)
        embed.add_field(name="Library", value=t.library or "-")
        embed.add_field(name="Rating", value=t.rating or "-")
        embed.add_field(name="Studio", value=t.studio or "-")
        embed.add_field(name="Genres", value=", ".join(t.genres) or "-", inline=False)
        embed.add_field(name="Suggested", value=self._names(pend["suggested"]), inline=False)
        embed.set_footer(text="Choose up to 3 channels, then Approve. It stays on New Arrivals until you decide.")
        msg = await (await self._channel()).send("**Where should this go?**", embed=embed, view=self._view(t, pend["selected"]))
        self.state.set_message(key, msg.id)

    def _view(self, t: Title, selected: list[str]) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        opts = [discord.SelectOption(label=name[:100], value=cid, default=cid in selected)
                for cid, name in self.rules.channel_choices(t)]
        v.add_item(discord.ui.Select(custom_id=f"ntva:sel:{t.key}", placeholder="Pick channels",
                                     min_values=1, max_values=min(self.rules.max, len(opts)), options=opts))
        v.add_item(discord.ui.Button(custom_id=f"ntva:ok:{t.key}", label="Approve", style=discord.ButtonStyle.success))
        v.add_item(discord.ui.Button(custom_id=f"ntva:skip:{t.key}", label="Leave on New Arrivals",
                                     style=discord.ButtonStyle.secondary))
        return v

    # ------------------------------------------------------------------ Discord input
    async def on_interaction(self, it: discord.Interaction) -> None:
        cid = (it.data or {}).get("custom_id", "")
        if not cid.startswith("ntva:"):
            return
        if CFG["allowed_users"] and it.user.id not in CFG["allowed_users"]:
            await it.response.send_message("You are not permitted to make allocation decisions.", ephemeral=True)
            return
        _, action, key = cid.split(":", 2)
        pend = self.state.get_pending(key)
        if not pend:
            await it.response.send_message("This title has already been handled.", ephemeral=True)
            return
        if action == "sel":
            self.state.set_selected(key, list(it.data.get("values", [])))
            await it.response.defer()
            return
        if action == "skip":
            self.state.mark_skipped(key, pend["label"])
            await it.response.edit_message(content=f"**{pend['label']}**: left on New Arrivals only. "
                                                   f"It returns here if it is still unassigned after New Arrivals drops it.",
                                           embed=None, view=None)
            return
        if action == "ok":
            chans = pend["selected"] or pend["suggested"]
            if not chans:
                await it.response.send_message("Pick at least one channel first.", ephemeral=True)
                return
            await it.response.defer()
            async with self.lock:
                if key not in self.titles:
                    t = await self.plex.title(key)
                    if t:
                        self.titles[key] = t
                await self.apply({key: chans[: self.rules.max]}, how=f"approved by {it.user.name}")
            await it.message.edit(content=f"**{pend['label']}**: added to {self._names(chans)}"
                                          + (" (dry run, nothing changed)" if CFG["dry_run"] else "") + ".",
                                  embed=None, view=None)


def main() -> None:
    missing = [k for k in ("PLEX_URL", "PLEX_TOKEN", "DISCORD_TOKEN", "DISCORD_CHANNEL_ID") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"missing required settings: {', '.join(missing)}")
    Allocator().run(CFG["discord_token"], log_handler=None)


if __name__ == "__main__":
    main()
