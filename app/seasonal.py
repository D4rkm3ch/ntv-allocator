"""Christmas and Halloween detection for the seasonal channels.

A season is identified by its Plex collection name. The NostalgiaTV channel for a season is found
automatically: any custom channel whose sources include a collection with that name. Plex
collections belong to one library, so each library gets its own collection of that name, created
when it is first needed.

Matching:
- the title (movie title or episode title) contains a clear word        -> added automatically
- only the summary does, or a weaker word appears in the summary         -> asked in Discord
Films merely set at Christmas, such as Die Hard, have no Christmas word in the title, so they are
always asked about rather than added.
"""
from __future__ import annotations

import re

SEASONS = {"christmas": "Christmas", "halloween": "Halloween"}   # season -> collection name

_CLEAR = {
    "christmas": re.compile(r"christmas|\bx-?mas\b|\byule(tide)?\b|\bsanta claus\b|\bsecret santa\b|"
                            r"\bgrinch\b|\bkrampus\b|\bnativity\b", re.I),
    "halloween": re.compile(r"\bhallowe'?en|\bhalloveen\b|\bhowl-?o-?ween\b|treehouse of horror|"
                            r"trick[- ]or[- ]treat|\ball hallows\b", re.I),
}
_WEAK = {   # only used on summaries
    "christmas": re.compile(r"\bsanta\b|\breindeer\b|\bmistletoe\b|\bnorth pole\b|\bchristmas eve\b", re.I),
    "halloween": re.compile(r"\bcostume party\b|\bjack-o'?-lanterns?\b|\bpumpkin patch\b|\bhaunted house\b", re.I),
}

DEFAULT_IGNORE = ["Classicals", "Fan Edits", "Reality Shows", "Music"]


def classify(title: str, summary: str = "") -> dict[str, str]:
    """season -> "clear" (add automatically) or "possible" (ask). Seasons that do not match are absent."""
    out = {}
    for season, rx in _CLEAR.items():
        if rx.search(title or ""):
            out[season] = "clear"
        elif rx.search(summary or "") or _WEAK[season].search(summary or ""):
            out[season] = "possible"
    return out


def chunks(items: list, size: int = 25) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]
