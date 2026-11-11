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
    "reask_days": int(env("REASK_DAYS", env("NEW_ARRIVALS_DAYS", "30"))),
    "cleanup": env("CLEANUP", "true").lower() in ("1", "true", "yes"),
    "cleanup_max": int(env("CLEANUP_MAX_REMOVALS", "50")),
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
        self.missing_since: dict[str, float] = {}   # rating key -> first scan it was seen missing from Plex
        self.mass_warned = False

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        if not CFG["dry_run"]:
            n = self.state.clear_dry_run()
            if n:
                log.info("cleared %d dry-run decisions; they will be applied on this scan", n)
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
            if CFG["cleanup"]:
                try:
                    await self.cleanup()
                except Exception:
                    log.exception("clean-up failed; nothing was asked or removed this pass")
            await asyncio.sleep(CFG["interval"] * 60)

    # ------------------------------------------------------------------ scanning
    async def scan(self) -> None:
        async with self.lock:
            unassigned = set(await self.ntv.unassigned_keys())
            recent = set(await self.plex.recently_added(CFG["lookback"], self.rules.ignore_libraries))
            # a title the server reports unassigned is re-evaluated even if handled before
            for k in unassigned:
                self.state.forget_resolved(k, CFG["reask_days"])
            candidates = [k for k in unassigned | recent if not self.state.is_known(k)]
            log.info("scan: %d unassigned, %d recent, %d to evaluate", len(unassigned), len(recent), len(candidates))
            if not candidates:
                await self._post_unsent()
                return

            config_homes, manual, hidden = await self.ntv.config_homes(self.rules.not_a_home)
            auto: dict[str, list[str]] = {}
            for key in candidates:
                try:
                    await self._evaluate(key, config_homes, manual, hidden, auto)
                except Exception:
                    # one bad title must not stop the rest of the scan; it is retried next scan
                    log.exception("could not evaluate rating key %s", key)

            if auto:
                await self.apply(auto, how="auto")
            await self._post_unsent()

    async def cleanup(self) -> list[str]:
        """Find titles that have left Plex but are still on channel manual lists, and ask in Discord
        whether to remove them: one question per show or movie (channels hold whole shows, so a
        deleted show is one question however many episodes it had; deleting some episodes of a show
        that is still in Plex needs nothing).

        Safeguards: only keys from this Plex server are considered; a key must be missing from
        every library listing AND answer 404 on two consecutive passes; a pass that finds more than
        CLEANUP_MAX_REMOVALS new titles asks nothing (a library may be unmounted).
        Returns the rating keys asked about this pass."""
        async with self.lock:
            lists = await self.ntv.manual_lists()
            prefix = f"{self.plex.machine_id}:"
            listed: dict[str, list[tuple[str, str]]] = defaultdict(list)   # key -> [(channel, stored)]
            for ch, stored in lists.items():
                for s in stored:
                    if s.startswith(prefix) and s[len(prefix):].isdigit():
                        listed[s[len(prefix):]].append((ch, s))
            present = await self.plex.library_index()
            # remember names while titles exist, so a deleted title can still be named
            self.state.remember_labels({k: v for k, v in present.items() if k in listed})
            gone = []
            for key in sorted(k for k in listed if k not in present):
                if not await self.plex.exists(key):
                    gone.append(key)
            now = asyncio.get_running_loop().time()
            # forget anything that came back; remember first sighting of new ones
            self.missing_since = {k: self.missing_since.get(k, now) for k in gone}
            for key, label, mid in self.state.drop_removal_questions_except(set(gone)):
                log.info("clean-up: %s is back in Plex; closing its removal question", label)
                if mid:
                    try:
                        ch = await self._channel()
                        msg = await ch.fetch_message(int(mid))
                        await msg.edit(content=f"**{label}** is back in Plex; nothing to remove.", view=None)
                    except Exception:
                        log.warning("could not update the removal question for %s", label)
            if not gone:
                return []
            confirmed = [k for k in gone if self.missing_since[k] < now]
            new = [k for k in confirmed if not self.state.removal_status(k)]
            log.info("clean-up: %d title(s) missing from Plex, %d confirmed, %d new", len(gone), len(confirmed), len(new))
            if not new:
                return []
            if len(new) > CFG["cleanup_max"]:
                log.warning("clean-up: %d titles missing, above CLEANUP_MAX_REMOVALS=%d; asking nothing.",
                            len(new), CFG["cleanup_max"])
                if not self.mass_warned:
                    self.mass_warned = True
                    await (await self._channel()).send(
                        f"**Clean-up paused**: {len(new)} titles on channel lists are missing from Plex at once. "
                        f"That usually means a library or drive is unmounted, so nothing will be asked or removed. "
                        f"If the deletion was deliberate, raise CLEANUP_MAX_REMOVALS and restart.")
                return []
            self.mass_warned = False
            labels = await self._removed_labels(new)
            if CFG["dry_run"]:
                for k in new:
                    log.info("DRY RUN, would ask to remove %s from %s", labels[k], [c for c, _ in listed[k]])
                return []
            for k in new:
                self.state.add_removal(k, labels[k], listed[k])
                try:
                    await self._post_removal(k)
                except Exception:
                    log.exception("could not post removal question for %s", labels[k])
            return new

    async def _removed_labels(self, keys: list[str]) -> dict[str, str]:
        """Names for titles Plex no longer has: remembered names, then NostalgiaTV's content cache,
        then the allocator's own records."""
        out = {k: self.state.label_for(k) for k in keys}
        missing = [k for k in keys if not out[k]]
        if missing:
            try:
                for k, lbl in (await self.ntv.labels([self.plex.qualified(k) for k in missing])).items():
                    out[k.split(":")[-1]] = out.get(k.split(":")[-1]) or lbl
            except Exception:
                log.warning("could not read titles from NostalgiaTV for %s", missing)
        return {k: out.get(k) or f"Unknown title (rating key {k})" for k in keys}

    async def _post_removal(self, key: str) -> None:
        r = self.state.get_removal(key)
        names = self._names([c for c, _ in r["pairs"]])
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(custom_id=f"ntva:rm:{key}", label="Remove", style=discord.ButtonStyle.danger))
        v.add_item(discord.ui.Button(custom_id=f"ntva:keep:{key}", label="Keep", style=discord.ButtonStyle.secondary))
        msg = await (await self._channel()).send(
            f"**Deleted from Plex: {r['label']}**\nStill on: {names}\nRemove it from these channels?", view=v)
        self.state.set_removal_message(key, msg.id)

    async def _answer_removal(self, it: discord.Interaction, action: str, key: str) -> None:
        r = self.state.get_removal(key)
        if not r or r["status"] != "pending":
            await it.response.send_message("This title has already been handled.", ephemeral=True)
            return
        if action == "keep":
            self.state.set_removal_status(key, "kept")
            await it.response.edit_message(content=f"**{r['label']}**: kept on {self._names([c for c, _ in r['pairs']])}. "
                                                   f"It will not be asked about again.", view=None)
            return
        await it.response.defer()
        try:
            async with self.lock:
                if await self.plex.exists(key):
                    self.state.drop_removal(key)
                    await it.message.edit(content=f"**{r['label']}** is back in Plex; nothing removed.", view=None)
                    return
                await self.ntv.backup()
                done, failed = [], []
                for ch, stored in r["pairs"]:
                    try:
                        await self.ntv.remove_from_channel(ch, stored)
                        done.append(ch)
                        log.info("clean-up: removed %s from %s (approved by %s)", r["label"],
                                 self.rules.names.get(ch, ch), it.user.name)
                    except Exception:
                        log.exception("clean-up: could not remove %s from %s", r["label"], ch)
                        failed.append(ch)
                for ch in done:
                    await self.ntv.refresh_channel(ch)
        except Exception as exc:
            log.exception("removal of %s failed", r["label"])
            await it.followup.send(f"Could not remove this ({type(exc).__name__}). Nothing was recorded; "
                                   f"press Remove again to retry, or check the container logs.", ephemeral=True)
            return
        if failed:
            # keep the question open for the channels that failed
            self.state.set_removal_pairs(key, [(c, s) for c, s in r["pairs"] if c in failed])
            await it.followup.send(f"Removed from {self._names(done)}; failed on {self._names(failed)}. "
                                   f"Press Remove again to retry.", ephemeral=True)
            return
        self.state.set_removal_status(key, "removed")
        self.missing_since.pop(key, None)
        await it.message.edit(content=f"**{r['label']}**: removed from {self._names(done)}.", view=None)

    async def _evaluate(self, key: str, config_homes: dict, manual: set, hidden: set, auto: dict) -> None:
        t = await self.plex.title(key)
        if t is None or t.library in self.rules.ignore_libraries:
            self.state.mark_skipped(key, t.label if t else key)   # not ours; do not look it up again
            return
        self.titles[key] = t
        homes = sorted(config_homes.get(key, set()))
        hints: list[str] = []
        if not homes:
            # NostalgiaTV's lookup also matches automatic criteria (which do not apply to
            # manual-list channels) and hidden channels; only visible criteria channels count
            reported = await self.ntv.channels_containing(self.plex.qualified(key))
            homes = [c for c in reported if c not in self.rules.not_a_home and c not in manual and c not in hidden]
            # criteria matches on visible manual-list channels are useful suggestions (NostalgiaTV
            # knows networks, e.g. NBC for The Office, which Plex's studio field often lacks)
            hints = [c for c in reported if c in manual and c not in hidden and c in self.rules.names]
            ignored = [c for c in reported if c not in homes]
            if ignored:
                log.info("%s: lookup also reported %s (hidden, manual-list or non-home channels, ignored)",
                         t.label, ignored)
        if homes:
            log.info("%s: already on %s, nothing to do", t.label, homes)
            self.state.mark_done(key, t.label, homes, "already placed")
            return
        p = self.rules.place(t)
        if p.auto and p.channels:
            log.info("%s: automatic -> %s", t.label, p.channels)
            auto[key] = p.channels
            return
        # question: channels both agree on first, then NostalgiaTV's hints, then the rules' picks
        both = [c for c in p.channels if c in hints]
        suggested = list(dict.fromkeys(both + hints + p.channels))[: self.rules.max]
        log.info("%s: ask in Discord -> %s (rules %s, NostalgiaTV hints %s)", t.label,
                 suggested or "no suggestion", p.channels, hints)
        self.state.add_pending(key, t.label, suggested)

    async def _post_unsent(self) -> None:
        for pend in self.state.unsent():
            try:
                await self._post_question(pend)
            except Exception:
                log.exception("could not post question for %s", pend["label"])

    async def apply(self, placements: dict[str, list[str]], how: str) -> dict[str, list[str]]:
        """Add titles to channels. Returns key -> channels actually accepted, and posts a summary
        for automatic placements (including anything NostalgiaTV refused)."""
        by_channel: dict[str, list[str]] = defaultdict(list)
        for key, chans in placements.items():
            for c in chans:
                by_channel[c].append(self.plex.qualified(key))
        accepted: dict[str, list[str]] = {k: list(c) for k, c in placements.items()}
        refusals: list[str] = []
        if CFG["dry_run"]:
            log.info("DRY RUN, would add: %s", dict(by_channel))
        else:
            await self.ntv.backup()
            for ch, keys in by_channel.items():
                res = await self.ntv.add_to_channel(ch, keys)
                for r in (res or {}).get("refused") or []:
                    rk = str(r.get("ratingKey", "") if isinstance(r, dict) else r).split(":")[-1]
                    reason = r.get("reason", "no reason given") if isinstance(r, dict) else "no reason given"
                    log.warning("NostalgiaTV refused %s on %s: %s", rk, ch, reason)
                    if rk in accepted and ch in accepted[rk]:
                        accepted[rk].remove(ch)
                    label = self.titles[rk].label if rk in self.titles else rk
                    refusals.append(f"{label} on {self.rules.names.get(ch, ch)}: {reason}")
                await self.ntv.refresh_channel(ch)
        for key, chans in accepted.items():
            label = self.titles[key].label if key in self.titles else key
            if not chans:
                # nothing accepted: do not record it as placed; asked again after REASK_DAYS
                self.state.mark_skipped(key, label)
                continue
            # dry-run decisions are remembered (so they are not re-posted every scan) but flagged,
            # and cleared on the first start without DRY_RUN so they are applied for real
            self.state.mark_done(key, label, chans, f"dry run ({how})" if CFG["dry_run"] else how)
        if how == "auto":
            await self._post_summary({k: c for k, c in accepted.items() if c}, refusals)
        elif refusals:
            await (await self._channel()).send("**NostalgiaTV refused**\n" + "\n".join(refusals))
        return accepted

    # ------------------------------------------------------------------ Discord output
    def _names(self, chans: list[str]) -> str:
        return ", ".join(self.rules.names.get(c, c) for c in chans) or "none"

    async def _post_summary(self, placed: dict[str, list[str]], refusals: list[str]) -> None:
        lines = [f"{self.titles[k].label} [{self.titles[k].library}]: {self._names(c)}" for k, c in placed.items()]
        head = "Placed automatically" + (" (dry run, nothing changed)" if CFG["dry_run"] else "")
        ch = await self._channel()
        for i in range(0, len(lines), 20):
            await ch.send(f"**{head}**\n" + "\n".join(lines[i:i + 20]))
        if refusals:
            await ch.send("**NostalgiaTV refused**\n" + "\n".join(refusals))

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
        embed.set_footer(text="Choose up to 3 channels, then Approve, or Skip for now.")
        msg = await (await self._channel()).send("**Where should this go?**", embed=embed, view=self._view(t, pend["selected"]))
        self.state.set_message(key, msg.id)

    def _view(self, t: Title, selected: list[str]) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        choices = self.rules.channel_choices(t, first=selected)
        opts = [discord.SelectOption(label=name[:100], value=cid, default=cid in selected[: self.rules.max])
                for cid, name in choices]
        v.add_item(discord.ui.Select(custom_id=f"ntva:sel:{t.key}", placeholder="Pick channels",
                                     min_values=1, max_values=min(self.rules.max, len(opts)), options=opts))
        v.add_item(discord.ui.Button(custom_id=f"ntva:ok:{t.key}", label="Approve", style=discord.ButtonStyle.success))
        v.add_item(discord.ui.Button(custom_id=f"ntva:skip:{t.key}", label="Skip for now",
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
        if action in ("rm", "keep"):
            await self._answer_removal(it, action, key)
            return
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
            await it.response.edit_message(content=f"**{pend['label']}**: skipped for now. "
                                                   f"It is asked about again in {CFG['reask_days']} days if it "
                                                   f"still has no channel.",
                                           embed=None, view=None)
            return
        if action == "ok":
            chans = (pend["selected"] or pend["suggested"])[: self.rules.max]
            if not chans:
                await it.response.send_message("Pick at least one channel first.", ephemeral=True)
                return
            await it.response.defer()
            try:
                async with self.lock:
                    if key not in self.titles:
                        t = await self.plex.title(key)
                        if t:
                            self.titles[key] = t
                    accepted = await self.apply({key: chans}, how=f"approved by {it.user.name}")
            except Exception as exc:
                log.exception("approval of %s failed", pend["label"])
                await it.followup.send(f"Could not apply this ({type(exc).__name__}). Nothing was recorded; "
                                       f"press Approve again to retry, or check the container logs.", ephemeral=True)
                return
            done = accepted.get(key) or []
            text = (f"**{pend['label']}**: added to {self._names(done)}" if done
                    else f"**{pend['label']}**: NostalgiaTV refused every channel; it has no channel yet")
            await it.message.edit(content=text + (" (dry run, nothing changed)" if CFG["dry_run"] else "") + ".",
                                  embed=None, view=None)


def main() -> None:
    missing = [k for k in ("PLEX_URL", "PLEX_TOKEN", "DISCORD_TOKEN", "DISCORD_CHANNEL_ID") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"missing required settings: {', '.join(missing)}")
    Allocator().run(CFG["discord_token"], log_handler=None)


if __name__ == "__main__":
    main()
