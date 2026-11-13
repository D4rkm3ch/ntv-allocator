"""Behaviour settings, edited on the status page and stored in the state database.

On first start each setting is seeded once: from its old environment variable if present (so an
existing install keeps its values), otherwise from the default. After that the database is the
only source; the environment variables are ignored and can be removed from the compose file.
A fresh install therefore starts in dry run.
"""
from __future__ import annotations

import os

# key: (label, type, default, min, max, legacy environment variables, help text)
SPEC: dict[str, tuple] = {
    "dry_run":     ("Dry run", bool, True, None, None, ("DRY_RUN",),
                    "Post questions in Discord but change nothing in NostalgiaTV."),
    "cleanup":     ("Clean-up of deleted titles", bool, True, None, None, ("CLEANUP",),
                    "Ask in Discord to remove titles deleted from Plex from their channels."),
    "interval":    ("Scan interval (minutes)", int, 15, 1, 60, ("SCAN_INTERVAL_MINUTES",),
                    "How often Plex and NostalgiaTV are checked."),
    "lookback":    ("Look-back for new titles (days)", int, 35, 1, 90, ("LOOKBACK_DAYS",),
                    "How far back Plex's recently added list is read."),
    "reask_days":  ("Ask about skipped titles again after (days)", int, 30, 1, 365, ("REASK_DAYS", "NEW_ARRIVALS_DAYS"),
                    "A title you skipped is asked about again after this long if it still has no channel."),
    "cleanup_max": ("Clean-up safety limit (titles)", int, 50, 1, 1000, ("CLEANUP_MAX_REMOVALS",),
                    "More titles missing at once than this pauses clean-up (usually an unmounted drive)."),
    "alert_after": ("Alert after failed checks", int, 3, 1, 20, ("ALERT_AFTER_FAILURES",),
                    "Failed connection checks in a row (one minute apart) before a Discord alert."),
    "alert_remind": ("Reminder while a problem lasts (hours)", int, 6, 1, 168, ("ALERT_REMIND_HOURS",),
                     "How often a still-ongoing problem is posted again."),
}


def _parse(kind, raw):
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return int(float(str(raw).strip()))


def load(state, cfg: dict) -> list[str]:
    """Seed missing settings (environment first, then default), then copy all into cfg.
    Returns the keys that were seeded on this start."""
    seeded = []
    stored = state.get_settings()
    for key, (_, kind, default, lo, hi, envs, _) in SPEC.items():
        if key not in stored:
            raw = next((os.environ[e] for e in envs if os.environ.get(e, "").strip()), None)
            try:
                value = _clamp(_parse(kind, raw), lo, hi) if raw is not None else default
            except ValueError:
                value = default
            state.set_setting(key, value)
            stored[key] = value
            seeded.append(key)
        cfg[key] = _parse(kind, stored[key])
    return seeded


def _clamp(v, lo, hi):
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def validate(changes: dict) -> tuple[dict, list[str]]:
    """Return (clean values, errors) for settings submitted from the page."""
    clean, errors = {}, []
    for key, raw in changes.items():
        if key not in SPEC:
            errors.append(f"unknown setting {key}")
            continue
        label, kind, _, lo, hi, _, _ = SPEC[key]
        try:
            v = _parse(kind, raw)
        except (TypeError, ValueError):
            errors.append(f"{label}: not a number")
            continue
        if kind is int and ((lo is not None and v < lo) or (hi is not None and v > hi)):
            errors.append(f"{label}: must be between {lo} and {hi}")
            continue
        clean[key] = v
    return clean, errors
