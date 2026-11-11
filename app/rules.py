"""Placement rules: turns a title's Plex metadata into channel suggestions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml


@dataclass
class Title:
    key: str                 # Plex ratingKey (unqualified)
    kind: str                # "show" | "movie"
    title: str
    year: int
    library: str
    studio: str = ""
    rating: str = ""
    genres: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Placement:
    channels: list[str]
    auto: bool
    reasons: list[str]


def _rx(pattern: str | None, value: str) -> bool:
    if not pattern:
        return True
    return re.search(pattern, value or "", re.I) is not None


class Rules:
    def __init__(self, path: str):
        with open(path, encoding="utf-8") as fh:
            self.cfg = yaml.safe_load(fh)
        self.max = int(self.cfg.get("max_channels_per_title", 3))
        self.names: dict[str, str] = {}
        self.groups: dict[str, dict[str, str]] = self.cfg["channels"]
        for grp in self.groups.values():
            self.names.update(grp)
        self.not_a_home = set(self.cfg.get("not_a_home", []))
        self.ignore_libraries = set(self.cfg.get("ignore_libraries", []))
        self._validate()

    # ------------------------------------------------------------------ helpers
    def _validate(self) -> None:
        """Fail fast if a rule references a channel that is not in the catalogue."""
        bad = set()

        def check(chs):
            for c in chs or []:
                if c not in self.names:
                    bad.add(c)

        for lib_rules in (self.cfg.get("shows") or {}).values():
            for r in lib_rules:
                check(r.get("channels"))
        m = self.cfg.get("movies") or {}
        check([m.get("stand_up_channel"), m.get("anime_classic_channel")])
        check(m.get("anime_channels"))
        for r in m.get("documentary", []) + m.get("curated", []):
            check(r.get("channels"))
        check(list(m.get("genre_primary", {}).values()) + list(m.get("secondary", {}).values()))
        check([e["channel"] for e in m.get("era", [])])
        for chs in (self.cfg.get("title_overrides") or {}).values():
            check(chs)
        if bad:
            raise ValueError(f"rules.yaml references unknown channels: {sorted(bad)}")

    @staticmethod
    def _match(rule: dict, t: Title) -> bool:
        if not _rx(rule.get("title"), t.title):
            return False
        if not _rx(rule.get("studio"), t.studio):
            return False
        if not _rx(rule.get("summary"), t.summary):
            return False
        if "rating_any" in rule and (t.rating or "") not in rule["rating_any"]:
            return False
        if "genres_any" in rule:
            want = {g.lower() for g in rule["genres_any"]}
            if not want & {g.lower() for g in t.genres}:
                return False
        if "year_min" in rule and (not t.year or t.year < rule["year_min"]):
            return False
        if "year_max" in rule and (not t.year or t.year > rule["year_max"]):
            return False
        return True

    def _cap(self, chans: list[str]) -> list[str]:
        out: list[str] = []
        for c in chans:
            if c not in out:
                out.append(c)
        return out[: self.max]

    def channel_choices(self, t: Title, first: list[str] | None = None) -> list[tuple[str, str]]:
        """Channels offered in the Discord picker (Discord allows 25). Suggested channels come first
        so they are always present."""
        if t.kind == "movie":
            order = ["movies", "adult_tv", "documentary", "anime", "kids"]
        elif t.library == "Anime":
            order = ["anime", "kids", "adult_tv"]
        elif t.library == "Documentary Shows":
            order = ["documentary", "adult_tv"]
        elif t.library == "Animated Shows":
            order = ["kids", "adult_tv", "anime"]
        else:
            order = ["adult_tv", "documentary", "kids"]
        out: list[tuple[str, str]] = [(c, self.names[c]) for c in (first or []) if c in self.names]
        for g in order:
            out.extend(item for item in self.groups.get(g, {}).items() if item not in out)
        return out[:25]

    # ------------------------------------------------------------------ placement
    def place(self, t: Title) -> Placement:
        overrides = self.cfg.get("title_overrides") or {}
        if t.label in overrides:
            return Placement(self._cap(overrides[t.label]), True, ["title override"])
        if t.kind == "movie":
            return self._place_movie(t)
        return self._place_show(t)

    def _place_show(self, t: Title) -> Placement:
        rules = (self.cfg.get("shows") or {}).get(t.library, [])
        chans: list[str] = []
        reasons: list[str] = []
        auto_hits = 0
        for i, r in enumerate(rules):
            if r.get("fallback") and reasons:
                continue
            if self._match(r, t):
                chans.extend(r["channels"])
                reasons.append(f"{t.library} rule {i + 1}")
                if r.get("auto"):
                    auto_hits += 1
        chans = self._cap(chans)
        # automatic only when every contributing rule is an auto rule
        return Placement(chans, bool(chans) and auto_hits == len(reasons), reasons)

    def _place_movie(self, t: Title) -> Placement:
        m = self.cfg["movies"]
        g = t.genres
        reasons: list[str] = []
        if t.library == m.get("stand_up_library"):
            return Placement([m["stand_up_channel"]], True, ["stand-up library"])
        if t.library == m.get("anime_library"):
            chans = list(m["anime_channels"])
            if _rx(m.get("anime_classic_title"), t.title) and m.get("anime_classic_title"):
                chans.append(m["anime_classic_channel"])
            return Placement(self._cap(chans), True, ["anime movie library"])
        if t.library == m.get("documentary_library"):
            for r in m.get("documentary", []):
                if self._match(r, t):
                    return Placement(self._cap(r["channels"]), True, ["documentary movie rule"])
        chans: list[str] = []
        for r in m.get("curated", []):
            if self._match(r, t):
                chans.extend(r["channels"])
                reasons.append("curated list")
        # primary genre
        prim = None
        if not g:
            # Plex has no genres: suggest from the curated lists only and ask in Discord
            return Placement(self._cap(chans), False, reasons + ["no genres in Plex"])
        for x in g:
            if x == "Animation":
                if t.rating in m.get("kid_ratings_for_animation", []) or (
                        t.rating == "Not Rated" and ({"Family", "Children"} & set(g))):
                    prim = "app_genre_family"
                    break
                if t.rating in ("R", "TV-MA", "NC-17"):
                    prim = "app_genre_comedy" if "Comedy" in g or "Adventure" in g else None
                    if prim:
                        break
                continue
            if x in m["genre_primary"]:
                prim = m["genre_primary"][x]
                break
        if not prim:
            if "Comedy" in g:
                prim = "app_genre_comedy"
            elif {"Crime", "Thriller", "Mystery"} & set(g):
                prim = "app_genre_thriller"
            else:
                prim = "app_genre_action"
        chans.append(prim)
        reasons.append("primary genre")
        for key, ch in m.get("secondary", {}).items():
            names = key.split("|")
            if any(n in g for n in names):
                if ch == "app_genre_thriller" and "Comedy" in g:
                    continue
                chans.append(ch)
        chans = list(dict.fromkeys(chans))
        era = None
        for e in m.get("era", []):
            if t.year and t.year >= e.get("year_min", 0) and t.year <= e.get("year_max", 9999):
                era = e["channel"]
                break
        if era:
            chans = chans[: self.max - 1] + [era]
        return Placement(self._cap(chans), True, reasons)
