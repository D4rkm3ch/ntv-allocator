"""NostalgiaTV Channel Allocator.

Finds shows and movies that have no permanent channel in NostalgiaTV, places them using
rules.yaml, and asks in Discord when a placement is not certain.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque

import discord

from .clients import NostalgiaTV, Plex
from .health import Health, describe
from . import status
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
    "status_port": int(env("STATUS_PORT", "19860") or 0),
    # off by default: every new show or movie is asked about in Discord
    "auto_place": env("AUTO_PLACE", "false").lower() in ("1", "true", "yes"),
    "edit_key": env("STATUS_EDIT_KEY", ""),
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
        self.health = Health(threshold=int(env("ALERT_AFTER_FAILURES", "3")),
                             remind_hours=float(env("ALERT_REMIND_HOURS", "6")))
        self.profile_checked = False
        # for the status page
        self.cfg = CFG
        self.checks: dict[str, dict] = {}
        self.last_scan: dict = {}
        self.scan_summary = ""
        self.next_run: float | None = None
        self.recent_events: deque = deque(maxlen=50)
        self.started = time.time()
        self.library_cache: tuple[float, list[dict]] = (0.0, [])

    def note(self, text: str) -> None:
        """Recent activity for the status page."""
        self.recent_events.append((time.time(), text.replace("**", "")))

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        if not CFG["dry_run"]:
            n = self.state.clear_dry_run()
            if n:
                log.info("cleared %d dry-run decisions; they will be applied on this scan", n)
        if CFG["status_port"]:
            try:
                await status.start(self, CFG["status_port"])
            except Exception:
                log.exception("could not start the status page on port %d", CFG["status_port"])
        self.loop.create_task(self._scan_loop())

    async def on_ready(self) -> None:
        log.info("Discord connected as %s", self.user)

    async def _channel(self) -> discord.abc.Messageable:
        ch = self.get_channel(CFG["discord_channel"]) or await self.fetch_channel(CFG["discord_channel"])
        return ch

    async def _probe(self) -> bool:
        """Check Plex and NostalgiaTV. Returns True when both answer."""
        good = True
        for name, url, probe in (("Plex", CFG["plex_url"], self.plex.probe),
                                 ("NostalgiaTV", CFG["ntv_url"], self.ntv.probe)):
            try:
                await probe()
                self.health.ok(name)
                self.checks[name] = {"ok": True, "at": time.time()}
            except Exception as exc:
                reason = describe(exc, url)
                log.error("%s check failed: %s", name, reason)
                self.health.fail(name, reason)
                prev = self.checks.get(name, {})
                since = prev.get("since") if prev.get("ok") is False else time.time()
                self.checks[name] = {"ok": False, "at": time.time(), "reason": reason, "since": since}
                good = False
        if good and not self.profile_checked:
            n = self.state.switch_profile(self.ntv.profile_id)
            if n:
                log.info("NostalgiaTV profile is now %s; cleared %d earlier placement records so recent titles "
                         "are re-checked against it", self.ntv.profile_id, n)
            self.profile_checked = True
        return good

    async def _flush_alerts(self) -> None:
        while self.health.outbox:
            try:
                await (await self._channel()).send(self.health.outbox[0])
            except Exception:
                log.exception("could not post an alert to Discord; retrying next check")
                return
            self.note(self.health.outbox.pop(0))

    async def run_once(self) -> bool:
        """One cycle: check connections, then scan and clean up if both answer. Returns True when
        the cycle ran normally (so the next one waits the full interval)."""
        if not await self._probe():
            return False
        try:
            await self.scan()
            if CFG["cleanup"]:
                await self.cleanup()
            self.health.ok("Allocator scans")
            self.last_scan = {"at": time.time(), "ok": True, "summary": self.scan_summary}
        except Exception as exc:
            log.exception("scan or clean-up failed")
            # a one-off error is retried quietly; two in a row is reported
            self.health.fail("Allocator scans", describe(exc) + " (details in the container log)", threshold=2)
            self.last_scan = {"at": time.time(), "ok": False, "reason": describe(exc)}
        return True

    async def _scan_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            healthy = await self.run_once()
            await self._flush_alerts()
            # while something is down, check every minute so recovery is noticed quickly
            wait = CFG["interval"] * 60 if healthy else 60
            self.next_run = time.time() + wait
            await asyncio.sleep(wait)

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
            self.scan_summary = f"{len(recent)} recent, {len(unassigned)} unassigned, {len(candidates)} new to check"
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
            # forget placement records of titles deleted from Plex (keeps Recent placements accurate)
            for k in [k for k in self.state.done_keys() if k not in present]:
                if not await self.plex.exists(k):
                    log.info("clean-up: %s is no longer in Plex; forgetting its placement record",
                             (self.state.get_done(k) or {}).get("label", k))
                    self.state.drop_done(k)
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
        self.note(f"Asked whether to remove {r['label']} (deleted from Plex)")

    async def _answer_removal(self, it: discord.Interaction, action: str, key: str) -> None:
        r = self.state.get_removal(key)
        if not r or r["status"] != "pending":
            await it.response.send_message("This title has already been handled.", ephemeral=True)
            return
        if action == "keep":
            self.state.set_removal_status(key, "kept")
            self.note(f"{r['label']}: kept on its channels ({it.user.name})")
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
                # only channels it is still on (you may have removed some yourself since the question)
                current = await self.ntv.manual_lists()
                still = [(c, st) for c, st in r["pairs"] if st in current.get(c, [])]
                done, failed = [c for c, st in r["pairs"] if (c, st) not in still], []
                if still:
                    await self.ntv.backup()
                for ch, stored in still:
                    try:
                        await self.ntv.remove_from_channel(ch, stored)
                        done.append(ch)
                        log.info("clean-up: removed %s from %s (approved by %s)", r["label"],
                                 self.rules.names.get(ch, ch), it.user.name)
                    except Exception:
                        log.exception("clean-up: could not remove %s from %s", r["label"], ch)
                        failed.append(ch)
                for ch in done:
                    if any(c == ch for c, _ in still):
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
        self.note(f"{r['label']}: removed from {self._names(done)} ({it.user.name})")
        self.missing_since.pop(key, None)
        await it.message.edit(content=f"**{r['label']}**: removed from {self._names(done)}.", view=None)

    async def edit_placement(self, key: str, channels: list[str]) -> dict:
        """Change which channels a placed title is on (from the status page). Adds use NostalgiaTV's
        add-to-channel and clear any exclusion; removals rewrite the channel's manual list (built-in
        channels) so no exclusion is created, or use remove-from-channel (custom channels)."""
        channels = [c for c in dict.fromkeys(channels) if c in self.rules.names]
        async with self.lock:
            row = self.state.get_done(key)
            label = row["label"] if row else (self.titles[key].label if key in self.titles else
                                              next((i["label"] for i in self.library_cache[1] if i["key"] == key), key))
            stored = self.plex.qualified(key)
            manual, excluded, custom = await self.ntv.lists_and_exclusions()
            current = [c for c in self.rules.names if stored in manual.get(c, [])]
            add = [c for c in channels if c not in current]
            drop = [c for c in current if c not in channels]
            if not add and not drop:
                return {"ok": True, "message": "No change.", "channels": current}
            if CFG["dry_run"]:
                return {"ok": True, "message": "Dry run: nothing changed.", "channels": current}
            await self.ntv.backup()
            done_add, done_drop, problems = [], [], []
            for c in add:
                try:
                    res = await self.ntv.add_to_channel(c, [stored])
                    if any(str(r.get("ratingKey")) == stored for r in (res or {}).get("refused") or []):
                        reason = next(r.get("reason") for r in res["refused"] if str(r.get("ratingKey")) == stored)
                        problems.append(f"{self.rules.names[c]} refused it ({reason})"); continue
                    done_add.append(c)
                    if stored in excluded.get(c, []):
                        if c in custom:
                            problems.append(f"{self.rules.names[c]} still excludes it; re-enable it in NostalgiaTV's channel editor")
                        else:
                            await self.ntv.set_override(c, excludedMediaRatingKeys=[k for k in excluded[c] if k != stored])
                except Exception as exc:
                    log.exception("status page: adding %s to %s failed", label, c)
                    problems.append(f"could not add to {self.rules.names[c]} ({type(exc).__name__})")
            for c in drop:
                try:
                    if c in custom:
                        await self.ntv.remove_from_channel(c, stored)
                    else:
                        await self.ntv.set_override(c, customMediaRatingKeys=[k for k in manual.get(c, []) if k != stored])
                    done_drop.append(c)
                except Exception as exc:
                    log.exception("status page: removing %s from %s failed", label, c)
                    problems.append(f"could not remove from {self.rules.names[c]} ({type(exc).__name__})")
            for c in done_add + done_drop:
                try:
                    await self.ntv.refresh_channel(c)
                except Exception:
                    log.warning("could not refresh %s", c)
            final = [c for c in current if c not in done_drop] + done_add
            how = (row["how"] if row else "manual")
            how = how if how.endswith("(edited on status page)") else f"{how} (edited on status page)"
            self.state.update_done(key, final, how) if row else self.state.mark_done(key, label, final, how)
            parts = []
            if done_add: parts.append("added to " + self._names(done_add))
            if done_drop: parts.append("removed from " + self._names(done_drop))
            msg = f"{label}: " + ("; ".join(parts) if parts else "nothing changed")
            self.note(msg + " (status page)")
            log.info("status page edit: %s%s", msg, f"; problems: {problems}" if problems else "")
            # keep the library view in step with the change
            for it in self.library_cache[1]:
                if it["key"] == key:
                    it["channels"] = final
            return {"ok": not problems, "message": msg + (". " + "; ".join(problems) if problems else "."), "channels": final}

    async def library_view(self, max_age: float = 60) -> list[dict]:
        """All managed shows and movies with their current catalogue channels (cached briefly)."""
        if time.time() - self.library_cache[0] < max_age and self.library_cache[1]:
            return self.library_cache[1]
        items = await self.plex.library_items(self.rules.ignore_libraries)
        manual, _, _ = await self.ntv.lists_and_exclusions()
        on: dict[str, list[str]] = defaultdict(list)
        prefix = f"{self.plex.machine_id}:"
        for ch in self.rules.names:
            for k in manual.get(ch, []):
                if k.startswith(prefix):
                    on[k[len(prefix):]].append(ch)
        for it in items:
            it["channels"] = on.get(it["key"], [])
        items.sort(key=lambda x: (x["label"].casefold()[4:] if x["label"].casefold().startswith("the ") else x["label"].casefold()))
        self.library_cache = (time.time(), items)
        return items

    async def current_channels(self, key: str) -> list[str]:
        manual, _, _ = await self.ntv.lists_and_exclusions()
        stored = self.plex.qualified(key)
        return [c for c in self.rules.names if stored in manual.get(c, [])]

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
        if p.auto and p.channels and CFG["auto_place"]:
            log.info("%s: automatic -> %s", t.label, p.channels)
            auto[key] = p.channels
            return
        # question: channels both agree on first; a confident rule's picks come next, otherwise
        # NostalgiaTV's hints, then the remaining picks
        both = [c for c in p.channels if c in hints]
        order = both + (p.channels + hints if p.auto else hints + p.channels)
        suggested = list(dict.fromkeys(order))[: self.rules.max]
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
            self.note(f"{label}: placed on {self._names(chans)} ({'dry run, ' if CFG['dry_run'] else ''}{how})")
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
        summary = t.summary.strip()
        if len(summary) > 240:                       # a short summary: cut at a word boundary
            summary = summary[:240].rsplit(" ", 1)[0].rstrip(",;:-") + "..."
        embed = discord.Embed(title=t.label, description=summary or "No summary in Plex.")
        embed.add_field(name="Library", value=t.library or "-")
        embed.add_field(name="Rating", value=t.rating or "-")
        embed.add_field(name="Studio", value=t.studio or "-")
        embed.add_field(name="Genres", value=", ".join(t.genres) or "-", inline=False)
        embed.add_field(name="Suggested", value=self._names(pend["suggested"]), inline=False)
        embed.set_footer(text="Choose up to 3 channels, then Approve, or Skip for now.")
        msg = await (await self._channel()).send("**Where should this go?**", embed=embed, view=self._view(t, pend["selected"]))
        self.note(f"Asked where {t.label} should go")
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
                    # always confirm the title still exists: it may have been deleted since the question
                    if not await self.plex.exists(key):
                        self.state.mark_skipped(key, pend["label"])
                        await it.message.edit(content=f"**{pend['label']}** is no longer in Plex; nothing added.",
                                              embed=None, view=None)
                        return
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
