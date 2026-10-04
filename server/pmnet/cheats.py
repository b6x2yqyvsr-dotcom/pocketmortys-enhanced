"""Gameplay overrides for a private server.

All of these default to OFF, so a checkout behaves like the reference
implementation unless an environment variable turns something on.  They exist
as one module because a private server is usually run by the same person who
plays it, and "make it generous" is a normal request -- but scattering magic
numbers through the gacha, registration, spawning and stat code would make the
server impossible to reason about afterwards.

Enable everything at once:

    PMNET_CHEAT=all python3 run.py

or individually:

    PMNET_CHEAT=gacha,level,maps,shiny,ivs

Individual flags take precedence, so `PMNET_CHEAT=gacha` plus
`PMNET_START_LEVEL=50` works as expected.

What "16/16/16/16" means
------------------------
``MortyData`` carries four per-Morty individual values -- ``hpIV``,
``attackIV``, ``defenceIV``, ``speedIV`` -- alongside four effort values.  The
colloquial "16/16/16/16" means *every IV maxed*, and 16 really is the ceiling:
the client's own signature is

    GenerateIVs(int min = 0, int max = 16)

so this is not an interpretation, it is the game's constant.

The IVs then feed the stat curve recovered from ``MortyData.SetLevel``, which
lives in :mod:`pmnet.gamedata` as :func:`~pmnet.gamedata.stats_for`.  So
enabling perfect IVs does not bolt a flat bonus onto the stats -- it makes the
client's real formula produce the perfect-IV result.
"""

from __future__ import annotations

import os

# The client's IV ceiling.  "16/16/16/16" is the community shorthand for a
# Morty with all four IVs maxed.
PERFECT_IV = 16


def _live(key: str) -> str | None:
    """Read a switch from the control panel's settings table.

    The panel writes to SQLite, so operators can flip a toggle and have it take
    effect on the next request without restarting.  Environment variables still
    work and still win, which keeps the documented CLI behaviour intact --
    ``PMNET_CHEAT=all python3 run.py`` must not be silently overridden by a row
    someone left behind in the database.
    """
    try:
        from . import telemetry
        if os.environ.get("PMNET_CHEAT") or os.environ.get(f"PMNET_{key.upper()}"):
            return None
        return telemetry.get(_SETTING_FOR.get(key, key), "")
    except Exception:  # noqa: BLE001
        return None


# ``_flag`` is keyed by the PMNET_CHEAT token (``gacha``, ``shiny``, ...), but
# the panel stores longer, more readable names.  Mapping explicitly beats
# guessing at a naming convention.
_SETTING_FOR = {
    "gacha": "free_gacha",
    "level": "start_level",
    "maps": "all_dimensions",
    "shiny": "all_shiny",
    "ivs": "perfect_ivs",
    "perfect": "perfect_ivs",
}


def _flag(name: str, token: str, default: bool = False) -> bool:
    """True if the panel switch is on, or `token` is in PMNET_CHEAT, or `name`
    is set to a truthy value."""
    live = _live(token)
    if live is not None and live != "":
        return live.strip().lower() in ("1", "true", "yes", "on")
    raw = os.environ.get(name)
    if raw is not None:
        return raw.strip().lower() not in ("", "0", "false", "no", "off")
    bundle = os.environ.get("PMNET_CHEAT", "")
    if not bundle:
        return default
    if bundle.strip().lower() in ("all", "1", "true", "yes", "on"):
        return True
    wanted = {p.strip().lower() for p in bundle.split(",") if p.strip()}
    return token.lower() in wanted


def _int(name: str, token: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            return default
    if _flag("__never__", token, False):
        return default
    return default


# ---- gacha --------------------------------------------------------------
# These are functions, not module constants, so the control panel's toggles
# take effect immediately.  Module-level constants were computed once at import
# and would have ignored every later change.

def free_gacha() -> bool:
    return _flag("PMNET_FREE_GACHA", "gacha")


# ---- accounts -----------------------------------------------------------

def start_level() -> int:
    """Level a brand-new account starts at."""
    live = _live("start_level")
    if live not in (None, ""):
        try:
            return max(1, int(live))
        except ValueError:
            pass
    raw = os.environ.get("PMNET_START_LEVEL")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 50 if _flag("__never__", "level", False) else 1


def starter_level() -> int:
    """The starter Morty's level -- never below 5.

    A level-1 Morty is almost unplayable under the client's own stat curve
    (HP 10, every other stat 5), which is what the reference implementation
    handed out.
    """
    return max(5, start_level())


def boost_starter() -> bool:
    return start_level() > 1


# ---- world --------------------------------------------------------------

def unlock_all_maps() -> bool:
    return _flag("PMNET_UNLOCK_ALL", "maps")


# ---- Mortys -------------------------------------------------------------

def all_shiny() -> bool:
    return _flag("PMNET_ALL_SHINY", "shiny")


def perfect_stats() -> bool:
    return (_flag("PMNET_PERFECT_IVS", "ivs")
            or _flag("PMNET_PERFECT_IVS", "perfect"))


# ---- starter grants (panel-editable) ------------------------------------

def welcome_coins() -> int:
    live = _live("welcome_coins")
    try:
        return int(live) if live not in (None, "") else 5000
    except ValueError:
        return 5000


def welcome_coupons() -> int:
    live = _live("welcome_coupons")
    try:
        return int(live) if live not in (None, "") else 50
    except ValueError:
        return 50


# ---- backward-compatible aliases ----------------------------------------
# Older call sites read these as attributes.  They resolve once at import,
# which is fine for a process started with PMNET_CHEAT set; anything that must
# react to panel changes should call the functions above.
FREE_GACHA = free_gacha()
START_LEVEL = start_level()
STARTER_LEVEL = starter_level()
BOOST_STARTER = boost_starter()
UNLOCK_ALL_MAPS = unlock_all_maps()
ALL_SHINY = all_shiny()
PERFECT_STATS = perfect_stats()


def perfect_bonus() -> int:
    """Stat bonus granted when every IV is maxed (0 when disabled)."""
    return PERFECT_IV if perfect_stats() else 0


def variant() -> str:
    """The variant every newly created or spawned Morty should have."""
    return "Shiny" if all_shiny() else "Normal"


def gacha_cost(cost: int) -> int:
    """Effective gacha price (0 when free pulls are enabled)."""
    if free_gacha():
        return 0
    try:
        return max(0, int(cost or 0))
    except (TypeError, ValueError):
        return 0


def describe() -> list[str]:
    """Human-readable summary, for the startup banner."""
    out = []
    if free_gacha():
        out.append("gacha is free")
    lvl = start_level()
    if lvl > 1:
        out.append(f"new accounts start at level {lvl}")
    if unlock_all_maps():
        out.append("all maps/dimensions unlocked")
    if all_shiny():
        out.append("every Morty is Shiny")
    if perfect_stats():
        out.append(f"every Morty has perfect IVs ({PERFECT_IV}/"
                   f"{PERFECT_IV}/{PERFECT_IV}/{PERFECT_IV})")
    return out
