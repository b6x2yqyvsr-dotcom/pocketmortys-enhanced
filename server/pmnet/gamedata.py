"""Accessors for the game's real data tables.

Everything here comes out of the shipped AssetBundles -- the client's own
``ItemInfo`` / ``AttackInfo`` / ``MortyInfo`` JSON, extracted by
:mod:`pmnet.assets`.  Nothing in this module is guessed.

What is real and what is not
----------------------------
Real:
  * the set of item ids, their rarity, effect type and effect value
  * each item's stack limit (``spbaglimit``)
  * every attack's pp, element and effect list with per-effect power/accuracy
  * every Morty's base stats and its level-gated learnset

Not real (yet):
  * the level -> stat curve.  The client computes it in
    ``GetBasicStatStepValue``, which is compiled IL2CPP; the exact coefficients
    need a code dump.  Until then :func:`basic_stats` uses a documented
    approximation, and the *inputs* it consumes are correct.

The original server's database is deliberately NOT used for stat curves: its
values are hardcoded per level (every Morty at level 5 has HP 20 regardless of
base stats), so they carry no information about the real formula.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache

from . import assets, config

MAX_ATTACKS = 4

# Learnset strings look like "AttackOutburst:1, AttackCry:6, AttackFlail:8".
_LEARN_RE = re.compile(r"\s*([A-Za-z0-9_]+)\s*:\s*(\d+)\s*")

# Effects look like "{Type:Hit, Power:25},{Type:Hit, Power:20, ToSelf:true}".
_EFFECT_RE = re.compile(r"\{([^}]*)\}")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def _load(name: str):
    """Load a table, preferring the single-player variant."""
    for sub in ("spdata", "mpdata", ""):
        path = assets.gamedata_dir() / sub / f"{name}.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return {}


@lru_cache(maxsize=1)
def items() -> dict:
    return _load("ItemInfo")


@lru_cache(maxsize=1)
def attacks() -> dict:
    return _load("AttackInfo")


@lru_cache(maxsize=1)
def mortys() -> dict:
    return _load("MortyInfo")


@lru_cache(maxsize=1)
def avatars() -> dict:
    return _load("PlayerAvatarInfo")


@lru_cache(maxsize=1)
def recipes() -> dict:
    return _load("RecipeInfo")


def reload() -> None:
    for fn in (items, attacks, mortys, avatars, recipes):
        fn.cache_clear()


def ready() -> bool:
    return bool(items()) and bool(mortys())


def ensure() -> bool:
    """Extract the tables if they have not been extracted yet."""
    if ready():
        return True
    assets.ensure_gamedata()
    reload()
    return ready()


# --------------------------------------------------------------------------
# localization
# --------------------------------------------------------------------------

#: Locale codes shipped in the ``text`` bundle.
LOCALES = ("ZH_CN", "ZH_TW", "EN", "JP", "KO")

_DEFAULT_LOCALE = "ZH_CN"


@lru_cache(maxsize=len(LOCALES) + 2)
def locale(code: str = _DEFAULT_LOCALE) -> dict:
    """Load a localisation table, falling back to English then to empty.

    The file has the shape::

        {"TextDefs": {...}, "Morty": {...}, "Item": {...},
         "Attack": {...}, "PlayerAvatar": {...}}

    where the per-entity sections map an id to ``{"name": ..., "description": ...}``.
    """
    for candidate in (code, "EN"):
        for sub in ("", "text"):
            path = assets.gamedata_dir() / sub / f"{candidate}.json"
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
    return {}


def localized(section: str, key: str, field: str = "name",
              code: str = _DEFAULT_LOCALE) -> str:
    """Look up one field of one entity, e.g. ``localized("Item", "ItemSerum")``."""
    entry = locale(code).get(section, {}).get(key)
    if isinstance(entry, dict):
        return str(entry.get(field) or "")
    if isinstance(entry, str):
        return entry
    return ""


def display_name(kind: str, key: str, code: str = _DEFAULT_LOCALE) -> str:
    """Human name for an item / Morty / attack / avatar, falling back to the id."""
    section = {"item": "Item", "morty": "Morty", "attack": "Attack",
               "avatar": "PlayerAvatar"}.get(kind.lower(), kind)
    return localized(section, key, "name", code) or key


def describe(kind: str, key: str, code: str = _DEFAULT_LOCALE) -> str:
    section = {"item": "Item", "morty": "Morty", "attack": "Attack",
               "avatar": "PlayerAvatar"}.get(kind.lower(), kind)
    return localized(section, key, "description", code)


def available_locales() -> list[str]:
    out = []
    for code in LOCALES:
        for sub in ("", "text"):
            if (assets.gamedata_dir() / sub / f"{code}.json").is_file():
                out.append(code)
                break
    return out


# --------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------

def item(item_id: str) -> dict:
    return items().get(item_id, {})


def stack_limit(item_id: str, default: int = 10) -> int:
    try:
        return int(item(item_id).get("spbaglimit") or default)
    except (TypeError, ValueError):
        return default


def item_effect(item_id: str) -> tuple[str, int]:
    """Return ``(effect_type, effect_value)``.

    This replaces the invented effect table the item handlers used to carry:
    the client's own ItemInfo declares what each item actually does.
    """
    entry = item(item_id)
    kind = str(entry.get("effecttype") or "")
    try:
        value = int(entry.get("effectvalue") or 0)
    except (TypeError, ValueError):
        value = 0
    return kind, value


def usable(item_id: str, where: str) -> bool:
    """``where`` is one of world / battle / craft."""
    key = {"world": "usableinworld", "battle": "usableinbattle",
           "craft": "usableincraft"}.get(where, "")
    if not key:
        return False
    return str(item(item_id).get(key, "")).upper() == "TRUE"


def gacha_items() -> list[str]:
    return [i for i, e in items().items()
            if str(e.get("includeingacha", "")).upper() == "TRUE"]


# --------------------------------------------------------------------------
# attacks
# --------------------------------------------------------------------------

def parse_effects(raw: str) -> list[dict]:
    """Parse an AttackInfo ``effects`` string into structured effects."""
    out: list[dict] = []
    for body in _EFFECT_RE.findall(raw or ""):
        effect: dict = {}
        for part in body.split(","):
            if ":" not in part:
                continue
            key, _, value = part.partition(":")
            key = key.strip()
            value = value.strip()
            low = value.lower()
            if low == "true":
                effect[key] = True
            elif low == "false":
                effect[key] = False
            else:
                try:
                    effect[key] = int(value)
                except ValueError:
                    try:
                        effect[key] = float(value)
                    except ValueError:
                        effect[key] = value
        if effect:
            out.append(effect)
    return out


def attack(attack_id: str) -> dict:
    entry = attacks().get(attack_id, {})
    if not entry:
        return {}
    return {
        "id": entry.get("id", attack_id),
        "element": entry.get("elementtype", ""),
        "pp": int(entry.get("pp") or 0),
        "effects": parse_effects(entry.get("effects", "")),
    }


def attack_power(attack_id: str) -> int:
    """Strongest Hit power in the attack's effect list (0 for status moves)."""
    best = 0
    for effect in attack(attack_id).get("effects", []):
        if str(effect.get("Type", "")).lower() == "hit":
            try:
                best = max(best, int(effect.get("Power") or 0))
            except (TypeError, ValueError):
                continue
    return best


# --------------------------------------------------------------------------
# Mortys
# --------------------------------------------------------------------------

def parse_learnset(raw: str) -> list[tuple[str, int]]:
    """``"AttackOutburst:1, AttackCry:6"`` -> ``[("AttackOutburst", 1), ...]``."""
    pairs = [((m.group(1)), int(m.group(2))) for m in _LEARN_RE.finditer(raw or "")]
    return sorted(pairs, key=lambda p: p[1])


def learnset(morty_id: str, level: int) -> list[str]:
    """The (up to four) attacks a Morty of ``level`` should know.

    The client keeps the most recently learned moves, which is what this
    reproduces: take everything learnable at or below ``level`` and keep the
    four with the highest learn level.
    """
    entry = mortys().get(morty_id)
    if not entry:
        return []
    known = [(aid, lvl) for aid, lvl in parse_learnset(entry.get("attacks", ""))
             if lvl <= level]
    known.sort(key=lambda p: p[1])
    chosen = known[-MAX_ATTACKS:]
    return [aid for aid, _ in chosen] or ["AttackStruggle"]


def base_stats(morty_id: str) -> dict:
    entry = mortys().get(morty_id, {})

    def num(key: str, default: int = 45) -> int:
        try:
            return int(entry.get(key) or default)
        except (TypeError, ValueError):
            return default

    return {
        "hp": num("hpbase"),
        "attack": num("attackbase"),
        "defence": num("defencebase"),
        "speed": num("speedbase"),
    }


# The level -> stat curve the client applies in GetBasicStatStepValue is not
# recoverable from the metadata (it is compiled code).  This approximation
# keeps the *shape* right -- stats grow with level and scale with base stats --
# --------------------------------------------------------------------------
# stat computation -- recovered from the client's own code
# --------------------------------------------------------------------------
#
# These constants are read out of ``MortyData.SetLevel`` in libil2cpp.so
# (RVA 0x166F974), not guessed.  The method inlines the same four-stat pattern
# four times:
#
#     evPart = floor(sqrt(EV) * 0.25)
#     hpStat      = floor(level * (hpBase      + hpIV      + evPart + 50) / 100) + 10
#     attackStat  = floor(level * (attackBase  + attackIV  + evPart)      / 100) + 5
#     defenceStat = floor(level * (defenceBase + defenceIV + evPart)      / 100) + 5
#     speedStat   = floor(level * (speedBase   + speedIV   + evPart)      / 100) + 5
#
# The multiplication by the reciprocal 0x51EB851F followed by ``asr #36`` is
# the compiler's strength-reduced signed division by 100; ``+50`` appears only
# in the HP branch.  ``level`` is clamped to [0, 100] by the same method.
#
# The IV ceiling comes from ``GenerateIVs(int min = 0, int max = 16)``, and the
# EV scale from ``GenerateEVs`` (a 20000.0f literal at VA 0x874FBC, divided by
# 100 in code): EV = max(0, (level - 5) * 200).

LEVEL_MAX = 100
IV_MAX = 16                # GenerateIVs(int min = 0, int max = 16)
EV_PER_LEVEL = 200         # 20000.0f / 100 in GenerateEVs
EV_START_LEVEL = 5
HP_FLAT = 10
STAT_FLAT = 5
HP_BASE_BONUS = 50


def ev_for_level(level: int) -> int:
    """The EV a freshly created Morty of this level is given."""
    return max(0, (min(int(level), LEVEL_MAX) - EV_START_LEVEL) * EV_PER_LEVEL)


def ev_part(ev: float) -> int:
    """``floor(sqrt(EV) * 0.25)`` -- the EV term in the stat formula."""
    if ev <= 0:
        return 0
    return int(math.floor(math.sqrt(ev) * 0.25))


def stats_for(morty_id: str, level: int, iv: int | None = None,
              ev: int | None = None) -> dict:
    """Compute a Morty's four stats exactly as the client does.

    ``iv`` defaults to the private-server setting (0 normally, 16 when the
    "perfect IVs" cheat is on); ``ev`` defaults to what the client itself
    derives from the level.
    """
    from . import cheats

    base = base_stats(morty_id)
    level = max(0, min(int(level), LEVEL_MAX))
    if iv is None:
        iv = cheats.PERFECT_IV if cheats.PERFECT_STATS else 0
    iv = max(0, min(int(iv), IV_MAX))
    if ev is None:
        ev = ev_for_level(level)
    evterm = ev_part(ev)

    hp = (level * (base["hp"] + iv + evterm + HP_BASE_BONUS)) // 100 + HP_FLAT
    atk = (level * (base["attack"] + iv + evterm)) // 100 + STAT_FLAT
    dfn = (level * (base["defence"] + iv + evterm)) // 100 + STAT_FLAT
    spd = (level * (base["speed"] + iv + evterm)) // 100 + STAT_FLAT
    return {"hp": hp, "hp_stat": hp, "attack_stat": atk,
            "defence_stat": dfn, "speed_stat": spd}


def basic_stats(morty_id: str, level: int) -> dict:
    """Backwards-compatible alias for :func:`stats_for`."""
    return stats_for(morty_id, level)


def xp_bounds(level: int) -> tuple[int, int]:
    """XP at this level and the next, matching the shape the client expects."""
    level = max(1, int(level))
    lower = level * level * 28
    upper = (level + 1) * (level + 1) * 28
    return lower, upper


# --------------------------------------------------------------------------
# combat -- also recovered from the client
# --------------------------------------------------------------------------
#
# The type chart is literally rock-paper-scissors, which is the game's actual
# mechanic: every Morty and every damaging move is Rock, Paper or Scissors (or
# nothing).  ``MortyDefs.GetElementTypeModifier`` (RVA 0x166ECA0) is a chain of
# three string comparisons returning 1.75 for the winning direction, 0.75 for
# the losing one and 1.0 otherwise -- there is no 0.5 tier.
#
# Which constant is which type is not left to inference: the game's own battle
# tutorial says it outright --
#
#     BATTLE_TUTORIAL_3_3: 脏兮兮莫蒂的一个招式可以造成 石头 伤害，
#                          对 剪刀 种类的敌人效果绝佳。
#     ("a move dealing Rock damage is super effective against Scissors")
#
# and the code's first branch is const1 vs const3 => 1.75.  So
# const1=Rock, const2=Paper, const3=Scissors, giving the classic cycle.
ELEMENT_STRONG = 1.75
ELEMENT_WEAK = 0.75
ELEMENT_NEUTRAL = 1.0

#: (attacker, defender) -> multiplier, for the six decided matchups only.
ELEMENT_CHART: dict[tuple[str, str], float] = {
    ("Rock", "Scissors"): ELEMENT_STRONG,
    ("Paper", "Rock"): ELEMENT_STRONG,
    ("Scissors", "Paper"): ELEMENT_STRONG,
    ("Rock", "Paper"): ELEMENT_WEAK,
    ("Paper", "Scissors"): ELEMENT_WEAK,
    ("Scissors", "Rock"): ELEMENT_WEAK,
}


def element_modifier(attacking_type: str, defending_type: str) -> float:
    """Damage multiplier for a type matchup (1.0 when unrelated)."""
    return ELEMENT_CHART.get((attacking_type or "", defending_type or ""),
                             ELEMENT_NEUTRAL)


def morty_element(morty_id: str) -> str:
    """A Morty's own element (Rock / Paper / Scissors, or '' for typeless)."""
    entry = mortys().get(morty_id) or {}
    return str(entry.get("elementtype") or "")


def attack_element(attack_id: str) -> str:
    entry = attacks().get(attack_id) or {}
    return str(entry.get("elementtype") or "")


# ``MortyDefs.ApplyDamage`` (RVA 0x166EE30) constants.  The magic divisors are
# in the instruction stream: 250.0f, +10 in the level term, +2 at the end, and
# a Unity ``Random.Range(0.85f, 1.0f)`` variance read from a literal at
# VA 0x87503C.
DAMAGE_DIVISOR = 250.0
DAMAGE_FLAT = 2.0
CRIT_LEVEL_MULTIPLIER = 1.75
VARIANCE_LOW = 0.85
VARIANCE_HIGH = 1.0


def damage(attack_stat: float, defence_stat: float, level: int, power: float,
           type_modifier: float = 1.0, crit: bool = False,
           defender_level: int | None = None,
           attack_stage: float = 0.0, defence_stage: float = 0.0,
           variance: float = VARIANCE_HIGH) -> int:
    """The client's damage calculation, ported from ``ApplyDamage``.

    ``attack_stage`` / ``defence_stage`` are the in-battle stat stages that
    :func:`stat_step_value` turns into multipliers.  ``variance`` is the
    ±15% roll; it defaults to the top of the range so callers that want a
    deterministic number (the server deciding an outcome) get one.
    """
    eff_level = float(level) * (CRIT_LEVEL_MULTIPLIER if crit else 1.0)

    # A higher-level defender is partly compensated for, with diminishing
    # effect: the attacker's effective level creeps up toward the defender's.
    if defender_level is not None and eff_level < defender_level:
        diff = float(defender_level) - eff_level
        eff_level += diff / (diff + 0.5)

    if defence_stat <= 0:
        defence_stat = 1.0
    ratio = attack_stat / defence_stat
    term = 1.0 + 0.5 * ratio

    atk_mult = stat_step_value(attack_stage)
    def_mult = stat_step_value(defence_stage) or 1.0

    base = (2.0 * eff_level + 10.0) / DAMAGE_DIVISOR
    base *= atk_mult / def_mult
    base *= term
    base *= float(power)
    base += DAMAGE_FLAT

    return int(math.floor(type_modifier * variance * base))


def stat_step_value(step: float) -> float:
    """``MortyDefs.GetBasicStatStepValue`` (RVA 0x166F6C8).

    Despite the name this is not the level curve -- it converts an in-battle
    stat stage into a multiplier.  The stage is floored and clamped to
    [-6, +6], and the curve is asymmetric:

        +1 -> 1.25   +2 -> 1.50   +6 -> 2.50
        -1 -> 0.80   -2 -> 0.67   -6 -> 0.40
    """
    stage = int(math.floor(step))
    stage = max(-6, min(6, stage))
    if stage >= 1:
        return (stage + 4) / 4.0
    if stage <= -1:
        return 4.0 / (abs(stage) + 4)
    return 1.0
