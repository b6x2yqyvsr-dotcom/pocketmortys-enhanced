"""Room entity spawning and snapshot rebuilding.

This is a faithful Python port of two PHP reference files:

* ``lib/room_entities.php`` -- :func:`spawn_room` is ``seed_room_entities()``
  (populate an *empty* room) and :func:`build_room_snapshot` is
  ``build_room_snapshot_from_events()``.
* ``lib/room-spawner.php`` -- :func:`top_up_room` is the per-room body of its
  main loop (bring an existing room up to ``TARGET_*`` counts), and
  :func:`get_room_active_state` is ``getRoomActiveState()``.

The PHP source disagrees with itself about the wild-Morty / bot counts, so both
rule sets are kept side by side and clearly labelled:

======================  ===========  ==========  ==========
source                  pickups      wilds       bots
======================  ===========  ==========  ==========
``seed_room_entities``  3            5           4
``room-spawner.php``    3            4           5
======================  ===========  ==========  ==========

:func:`spawn_room` follows ``seed_room_entities`` (its signature is an exact
match for ``spawn_room(room_id, world_id, zone_id)``); :func:`top_up_room`
follows ``room-spawner.php``.

Everything here is pure data: no HTTP, no :func:`pmnet.config.public_base`.
"""

from __future__ import annotations

import json
import random
import zlib
from typing import Any

from . import cheats, db, events

__all__ = [
    "spawn_room",
    "build_room_snapshot",
    "ensure_room_ready",
    "get_room_active_state",
    "top_up_room",
    "room_has_entities",
    "SEED_ROOM",
]

# --------------------------------------------------------------------------
# Config (room-spawner.php)
# --------------------------------------------------------------------------

# Top-up targets used by room-spawner.php's main loop.
TARGET_PICKUPS_PER_ROOM = 3
TARGET_WILD_MORTIES_ROOM = 4
TARGET_BOTS_PER_ROOM = 5

# How many events room-spawner.php replays to rebuild "active" state.
MAX_SCAN_EVENTS = 3000

# Counts used by seed_room_entities() when filling a brand-new room.
# These are the compiled-in defaults; the control panel can override each one
# (settings keys ``room_pickups`` / ``room_wilds`` / ``room_bots``).
SEED_PICKUPS = 3
SEED_WILD_MORTIES = 5
SEED_BOTS = 4


def _seed_count(setting: str, default: int) -> int:
    """Panel override for a seed count, clamped to the spawn-point count."""
    try:
        from . import telemetry
        n = telemetry.get_int(setting, default)
    except Exception:  # noqa: BLE001
        n = default
    return max(0, min(int(n), 12))

# room_entities.php hard-codes level 5 for seeded bots; room-spawner.php rolls
# randInt(1, 5).  Kept apart so each port stays exact.
SEED_BOT_LEVEL = 5
BOT_LEVEL_MIN = 1
BOT_LEVEL_MAX = 5

# --------------------------------------------------------------------------
# Spawn points (identical in both PHP files)
# --------------------------------------------------------------------------

PICKUP_POINTS: list[list[int]] = [
    [25, 87],
    [34, 83],
    [42, 64],
    [48, 87],
    [57, 48],
    [65, 79],
]

MOB_POINTS: list[list[int]] = [
    [5, 82],
    [12, 84],
    [15, 4],
    [24, 57],
    [24, 76],
    [32, 76],
    [36, 59],
    [37, 76],
    [42, 75],
    [47, 68],
    [49, 84],
    [55, 79],
]

# --------------------------------------------------------------------------
# Pools
# --------------------------------------------------------------------------

# seed_room_entities() wild pool: variant is rolled (Shiny / Normal).
SEED_WILD_MORTY_POOL = [
    "MortyDefault",
    "MortyPrisoner",
    "MortySurvivor",
    "MortyCowboy",
    "MortyBlueShirt",
    "MortyNoEye",
]

# room-spawner.php wild pool: variant is *always* Normal.
SPAWNER_WILD_MORTY_POOL = [
    "MortyPrisoner",
    "MortyCrying",
    "MortyCrow",
    "MortyTeaCup",
    "MortySoldadoLoco",
    "MortyFelon",
    "MortyMulti",
    "MortyExoPrime",
    "MortyRobotChicken",
]

BOT_NAME_POOL = [
    "Ataraxy",
    "Carpedge",
    "ChloeTombola",
    "Loxodromy",
    "Barbirdation",
    "EasementJustice",
]

BOT_AVATAR_POOL = [
    "AvatarTeacherRick",
    "AvatarMoochJerry",
    "AvatarBeth",
    "AvatarRickSuperFan",
    "AvatarRickDefault",
]

BOT_MORTY_POOL = [
    "MortyPoorHouse",
    "MortyGunk",
    "MortySoldier",
    "MortyTyrantLizard",
    "MortyAndroid",
]

# The static owned-morty id both PHP files hard-code for bots.
BOT_OWNED_MORTY_ID = "80700000-0000-0000-0000-000000000000"

# Loot table (identical in room_entities.php and room-spawner.php).
LOOT_TABLE_BY_RARITY: dict[int, list[str]] = {
    5: [
        "ItemMegaSeedSpeed",
        "ItemTinCan",
        "ItemCircuitBoard",
        "ItemCable",
        "ItemPlutonicRock",
        "ItemBacteriaCell",
    ],
    75: ["ItemPoisonCure", "ItemCable", "ItemCircuitBoard"],
    100: ["ItemSerum", "ItemDarkEnergyBall", "ItemCircuitBoard", "ItemPlutonicRock"],
}

_LOOT_FALLBACK = ["ItemCircuitBoard"]

# Probability gates.  PHP compares ``mt_rand(0, 100) < N`` -- note the
# inclusive upper bound, i.e. 10/101 and 25/101, not exactly 10%/25%.
WILD_SHINY_THRESHOLD = 10
WILD_SHINY_IF_POTION_THRESHOLD = 25
WILD_DIVISION_MIN = 1
WILD_DIVISION_MAX = 4

# --------------------------------------------------------------------------
# room_ids seed row
# --------------------------------------------------------------------------

# Ensure a fresh server can hand out a room even before legacy import ran.
SEED_ROOM = {
    "room_id": "56092cc3-d968-4d2d-8c54-98ed0817hu97",
    "room_udp_host": "127.0.0.1",
    "room_udp_port": "13001",
    "world_id": "1",
    "zone_id": "[13-15]",
}

# Presence window used to rank rooms (PHP: ``NOW() - INTERVAL 5 MINUTE``).
PRESENCE_WINDOW_MINUTES = 5


# --------------------------------------------------------------------------
# small PHP-flavoured helpers
# --------------------------------------------------------------------------

def weighted_pick(choices: list[tuple[Any, int]], rng: random.Random) -> Any:
    """Port of ``weighted_pick()`` -- ``random_int(1, total)`` inclusive."""
    total = sum(int(w) for _, w in choices)
    roll = rng.randint(1, max(1, total))
    acc = 0
    for value, weight in choices:
        acc += int(weight)
        if roll <= acc:
            return value
    return choices[-1][0] if choices else None


def pick_rarity(rng: random.Random) -> int:
    """Port of ``pick_rarity()``: 5@55%, 75@30%, 100@15%."""
    return int(weighted_pick([(5, 55), (75, 30), (100, 15)], rng))


def pick_item_id(rarity: int, rng: random.Random,
                 exclude: list[str] | tuple[str, ...] = ()) -> str:
    """Port of ``pick_item_id()`` -- excludes are best-effort."""
    pool = list(LOOT_TABLE_BY_RARITY.get(int(rarity), _LOOT_FALLBACK))
    if exclude:
        filtered = [i for i in pool if i not in exclude]
        if filtered:
            pool = filtered
    return rng.choice(pool)


def random_pickup_contents(rng: random.Random,
                           exclude: list[str] | None = None) -> list[dict]:
    """Port of ``random_pickup_contents()``.

    Rule (from the PHP comment): NEVER coin-only; either a single ITEM or a
    bundle of 2-4 ITEMs plus one COIN.
    """
    exclude = list(exclude or [])
    kind = weighted_pick([("single", 70), ("bundle", 30)], rng)

    if kind == "single":
        rarity = pick_rarity(rng)
        item = pick_item_id(rarity, rng, exclude)
        return [{"type": "ITEM", "amount": 1, "item_id": item, "rarity": rarity}]

    n_items = rng.randint(2, 4)
    contents: list[dict] = []
    picked: list[str] = []
    for _ in range(n_items):
        rarity = pick_rarity(rng)
        item = pick_item_id(rarity, rng, exclude + picked)
        picked.append(item)
        contents.append({"type": "ITEM", "amount": 1, "item_id": item,
                         "rarity": rarity})
    contents.append({"type": "COIN", "amount": rng.randint(120, 250)})
    return contents


def _xy_key(point: Any) -> str:
    return f"{int(point[0])},{int(point[1])}"


def _pick_free_placement(points: list[list[int]], occupied: set[str],
                         rng: random.Random) -> list[int] | None:
    """Port of ``pickFreePlacement()``; ``None`` where PHP returns ``[]``."""
    available = [p for p in points if _xy_key(p) not in occupied]
    if not available:
        return None
    return list(rng.choice(available))


def _pick_from_pool(pool: list, base: list, rng: random.Random):
    """Port of ``spawn_pick_from_list()`` + the refill-when-empty rule."""
    if not pool:
        pool.extend(base)
    idx = rng.randrange(len(pool))
    point = pool.pop(idx)
    return point


def _spawn_rng(room_id: str) -> random.Random:
    """Deterministic per-room RNG.

    PHP does ``mt_srand((int) sprintf('%u', crc32($room_id)))``.  Python's MT
    stream is not PHP's, so the *sequence* cannot match, but seeding with the
    same CRC32 keeps the "same room -> same layout" intent.
    """
    return random.Random(zlib.crc32(room_id.encode("utf-8")) & 0xFFFFFFFF)


# --------------------------------------------------------------------------
# payload builders
# --------------------------------------------------------------------------

def _pickup_payload(placement: list[int], rng: random.Random,
                    exclude: list[str] | None = None) -> dict:
    return {
        "contents": random_pickup_contents(rng, exclude),
        "placement": [int(placement[0]), int(placement[1])],
        "pickup_id": events.new_id(),
    }


def _wild_morty_payload(morty_id: str, placement: list[int], rng: random.Random,
                        variant: str | None = None,
                        shiny_if_potion: bool | None = None) -> dict:
    """Build a full wild payload and round-trip it through the normaliser.

    The client silently refuses to make a wild Morty clickable unless every
    field (state/division/variant/timestamps) is present, so we never emit the
    shorthand form.
    """
    stamp = db.iso_now()
    if variant is None:
        variant = cheats.variant()
        if variant == "Normal":
            variant = ("Shiny" if rng.randint(0, 100) < WILD_SHINY_THRESHOLD
                       else "Normal")
    if shiny_if_potion is None:
        shiny_if_potion = (cheats.ALL_SHINY
                           or rng.randint(0, 100) < WILD_SHINY_IF_POTION_THRESHOLD)

    payload = {
        "morty_id": str(morty_id),
        "placement": [int(placement[0]), int(placement[1])],
        "state": "WORLD",
        "division": rng.randint(WILD_DIVISION_MIN, WILD_DIVISION_MAX),
        "variant": str(variant),
        "shiny_if_potion": bool(shiny_if_potion),
        "_created": stamp,
        "_updated": stamp,
        "wild_morty_id": events.new_id(),
    }
    # Round-trip so the exact field set is guaranteed even if this drifts.
    return json.loads(events.normalize_wild_morty(json.dumps(payload)))


def _bot_payload(placement: list[int] | None, rng: random.Random,
                 level: int) -> dict:
    zone_x = rng.randint(1, 5)
    zone_y = rng.randint(1, 5)
    stamp = db.iso_now()

    payload: dict = {
        "username": rng.choice(BOT_NAME_POOL),
        "player_avatar_id": rng.choice(BOT_AVATAR_POOL),
        "state": "WORLD",
        "level": int(level),
        "owned_morties": [{
            "morty_id": rng.choice(BOT_MORTY_POOL),
            "variant": "Normal",
            "hp": 1,
            "owned_morty_id": BOT_OWNED_MORTY_ID,
        }],
        "zone": {
            "player": [zone_x, zone_y],
            "bots": {
                "count": rng.randint(6, 12),
                "morty_count": {"min": 1, "max": 1},
                "morty_hp_handicap": {"min": 0.4, "max": 0.6},
            },
            "zone_id": f"[{zone_x}-{zone_y}]",
        },
        "streak": 0,
        "_created": stamp,
        "_updated": stamp,
        "bot_id": events.new_id(),
    }
    if placement is not None:
        # seed_room_entities() omits this; room-spawner.php and the client's
        # room view both expect a placement, so we always emit one.
        payload["placement"] = [int(placement[0]), int(placement[1])]
    return payload


# --------------------------------------------------------------------------
# 1. spawn_room -- port of seed_room_entities()
# --------------------------------------------------------------------------

def spawn_room(room_id: str, world_id: str, zone_id: str) -> dict:
    """Populate an *empty* room with pickups, wild Mortys and bots.

    Publishes ``room:pickup-added`` / ``room:wild-morty-added`` /
    ``room:bot-added`` into ``event_queue``.  Counts follow
    ``seed_room_entities()``: 3 pickups, 5 wild Mortys, 4 bots.

    ``room:initialized`` is *not* published, matching the PHP (the call is
    commented out there).

    Returns ``{"room_id", "world_id", "zone_id", "spawned", "occupied_xy"}``.
    """
    room_id = str(room_id)
    rng = _spawn_rng(room_id)
    occupied: set[str] = set()
    spawned = {"pickups": 0, "wilds": 0, "bots": 0}

    # --- pickups (pool without replacement, refilled if exhausted) ---
    item_pool = list(PICKUP_POINTS)
    excludes: list[str] = []
    for _ in range(_seed_count("room_pickups", SEED_PICKUPS)):
        point = _pick_from_pool(item_pool, list(PICKUP_POINTS), rng)
        payload = _pickup_payload(point, rng, excludes)
        events.publish(room_id, "room:pickup-added", payload)
        occupied.add(_xy_key(point))
        spawned["pickups"] += 1
        for c in payload["contents"]:
            if isinstance(c, dict) and c.get("type") == "ITEM" and c.get("item_id"):
                excludes.append(str(c["item_id"]))

    # --- wild Mortys (shared MOB_POINTS, no overlap with each other) ---
    morty_pool = list(MOB_POINTS)
    for _ in range(_seed_count("room_wilds", SEED_WILD_MORTIES)):
        point = _pick_from_pool(morty_pool, list(MOB_POINTS), rng)
        if _xy_key(point) in occupied:  # defensive; pools never overlap here
            point = _pick_free_placement(MOB_POINTS, occupied, rng)
            if point is None:
                break
        payload = _wild_morty_payload(rng.choice(SEED_WILD_MORTY_POOL),
                                      point, rng)
        events.publish(room_id, "room:wild-morty-added", payload)
        occupied.add(_xy_key(point))
        spawned["wilds"] += 1

    # --- bots (free MOB_POINTS not used by wilds) ---
    for _ in range(_seed_count("room_bots", SEED_BOTS)):
        point = _pick_free_placement(MOB_POINTS, occupied, rng)
        if point is None:
            break
        payload = _bot_payload(point, rng, SEED_BOT_LEVEL)
        events.publish(room_id, "room:bot-added", payload)
        occupied.add(_xy_key(point))
        spawned["bots"] += 1

    return {
        "room_id": room_id,
        "world_id": str(world_id),
        "zone_id": str(zone_id),
        "spawned": spawned,
        "occupied_xy": sorted(occupied),
    }


# --------------------------------------------------------------------------
# 2. snapshot rebuilding -- port of build_room_snapshot_from_events()
# --------------------------------------------------------------------------

_SNAPSHOT_EVENTS = (
    "room:pickup-added", "room:pickup-removed",
    "room:wild-morty-added", "room:wild-morty-removed",
    "room:wild-morty-state-changed",
    "room:bot-added", "room:bot-removed", "room:bot-state-changed",
)

# getRoomActiveState() replays these; kept separate for clarity.
_ACTIVE_EVENTS = (
    "room:pickup-added", "room:pickup-removed",
    "room:wild-morty-added", "room:wild-morty-removed",
    "room:bot-added", "room:bot-removed",
)


def _load_payload(raw: Any) -> dict | None:
    if raw is None:
        return None
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_room_snapshot(room_id: str) -> dict:
    """Rebuild the live entity list for a room by replaying ``event_queue``.

    Returns ``{"pickups": [...], "wild_morties": [...], "bots": [...]}`` -- the
    exact shape ``/session/join-room`` expects.

    A pickup is considered gone when its ``room:pickup-added`` row has a
    non-null ``pickup_id_collected_by_player_id``; ``room:pickup-removed`` also
    drops it.  State-changed events mutate ``state`` and ``_updated``.
    """
    pickups: dict[str, dict] = {}
    wilds: dict[str, dict] = {}
    bots: dict[str, dict] = {}

    placeholders = ",".join("?" * len(_SNAPSHOT_EVENTS))
    rows = db.all_(
        f"""SELECT event_name, payload_json, pickup_id_collected_by_player_id
              FROM event_queue
             WHERE room_id = ?
               AND event_name IN ({placeholders})
             ORDER BY id ASC""",
        (str(room_id), *_SNAPSHOT_EVENTS),
    )

    for row in rows:
        event = str(row["event_name"])
        payload = _load_payload(row["payload_json"])
        if payload is None:
            continue

        pickup_id = str(payload.get("pickup_id") or "")
        wild_id = str(payload.get("wild_morty_id") or "")
        bot_id = str(payload.get("bot_id") or "")

        if event == "room:pickup-added" and pickup_id:
            if row["pickup_id_collected_by_player_id"]:
                continue
            pickups[pickup_id] = payload

        elif event == "room:pickup-removed" and pickup_id:
            pickups.pop(pickup_id, None)

        elif event == "room:wild-morty-added" and wild_id:
            wilds[wild_id] = payload

        elif event == "room:wild-morty-removed" and wild_id:
            wilds.pop(wild_id, None)

        elif (event == "room:wild-morty-state-changed" and wild_id
              and wild_id in wilds and "state" in payload):
            wilds[wild_id]["state"] = payload["state"]
            wilds[wild_id]["_updated"] = db.iso_now()

        elif event == "room:bot-added" and bot_id:
            bots[bot_id] = payload

        elif event == "room:bot-removed" and bot_id:
            bots.pop(bot_id, None)

        elif (event == "room:bot-state-changed" and bot_id
              and bot_id in bots and "state" in payload):
            bots[bot_id]["state"] = payload["state"]
            bots[bot_id]["_updated"] = db.iso_now()

    return {
        "pickups": list(pickups.values()),
        "wild_morties": list(wilds.values()),
        "bots": list(bots.values()),
    }


# --------------------------------------------------------------------------
# active-state replay -- port of getRoomActiveState()
# --------------------------------------------------------------------------

def get_room_active_state(room_id: str) -> dict:
    """Replay up to ``MAX_SCAN_EVENTS`` events and return what is still live.

    Returns ``{"pickups", "wilds", "bots", "occupied_xy"}`` -- keyed by entity
    id, exactly like the PHP, plus an ``occupied_xy`` set.
    """
    rows = db.all_(
        """SELECT event_name, payload_json, pickup_id,
                  pickup_id_collected_by_player_id, id
             FROM event_queue
            WHERE room_id = ?
            ORDER BY id ASC
            LIMIT ?""",
        (str(room_id), MAX_SCAN_EVENTS),
    )

    active_pickups: dict[str, dict] = {}
    active_wilds: dict[str, dict] = {}
    active_bots: dict[str, dict] = {}

    for row in rows:
        event = str(row["event_name"])
        payload = _load_payload(row["payload_json"]) or {}

        if event == "room:pickup-added":
            pid = str(row["pickup_id"] or payload.get("pickup_id") or "")
            if pid:
                if row["pickup_id_collected_by_player_id"]:
                    active_pickups.pop(pid, None)
                else:
                    active_pickups[pid] = payload

        elif event == "room:pickup-removed":
            pid = str(payload.get("pickup_id") or "")
            if pid:
                active_pickups.pop(pid, None)

        elif event == "room:wild-morty-added":
            wid = str(payload.get("wild_morty_id") or "")
            if wid:
                active_wilds[wid] = payload

        elif event == "room:wild-morty-removed":
            wid = str(payload.get("wild_morty_id") or "")
            if wid:
                active_wilds.pop(wid, None)

        elif event == "room:bot-added":
            bid = str(payload.get("bot_id") or "")
            if bid:
                active_bots[bid] = payload

        elif event == "room:bot-removed":
            bid = str(payload.get("bot_id") or "")
            if bid:
                active_bots.pop(bid, None)

    occupied: set[str] = set()
    for group in (active_pickups, active_wilds, active_bots):
        for payload in group.values():
            placement = payload.get("placement")
            if isinstance(placement, (list, tuple)) and len(placement) >= 2:
                occupied.add(f"{int(placement[0])},{int(placement[1])}")

    return {
        "pickups": active_pickups,
        "wilds": active_wilds,
        "bots": active_bots,
        "occupied_xy": occupied,
    }


def room_has_entities(room_id: str) -> bool:
    """Port of join-room's ``room_has_entities()``: does the room have *any*
    live pickup / wild / bot event at all.

    This is deliberately the PHP's existence check (it does not subtract later
    removals); :func:`build_room_snapshot` is the authoritative live view.
    """
    row = db.one(
        """SELECT 1
             FROM event_queue
            WHERE room_id = ?
              AND (
                (event_name = 'room:pickup-added'
                 AND pickup_id_collected_by_player_id IS NULL)
                OR event_name = 'room:wild-morty-added'
                OR event_name = 'room:bot-added'
              )
            LIMIT 1""",
        (str(room_id),),
    )
    return row is not None


# --------------------------------------------------------------------------
# 3. ensure_room_ready
# --------------------------------------------------------------------------

def _ensure_seed_room() -> None:
    """Insert the seed ``room_ids`` row if the table exists but is empty."""
    try:
        row = db.one("SELECT COUNT(*) AS n FROM room_ids")
    except Exception:  # noqa: BLE001 -- table may not exist yet
        return
    if row is None or int(row["n"] or 0) > 0:
        return
    try:
        db.run(
            """INSERT INTO room_ids
                   (room_id, room_udp_host, room_udp_port, world_id, zone_id)
               VALUES (?,?,?,?,?)""",
            (SEED_ROOM["room_id"], SEED_ROOM["room_udp_host"],
             SEED_ROOM["room_udp_port"], SEED_ROOM["world_id"],
             SEED_ROOM["zone_id"]),
        )
    except Exception:  # noqa: BLE001
        pass


def _candidate_rooms(world_id: str, zone_id: str = "") -> list[Any]:
    """Rooms for ``world_id`` (falling back to all), narrowed by ``zone_id``."""
    try:
        rows = db.all_(
            """SELECT room_id, room_udp_host, room_udp_port, world_id, zone_id
                 FROM room_ids
                ORDER BY room_id ASC""",
        )
    except Exception:  # noqa: BLE001
        return []
    if not rows:
        return []

    if world_id:
        matching = [r for r in rows if str(r["world_id"] or "") == str(world_id)]
        wide = matching or list(rows)
    else:
        wide = list(rows)

    if zone_id:
        narrow = [r for r in wide if str(r["zone_id"] or "") == str(zone_id)]
        if narrow:
            return narrow
    return wide


def _presence_counts() -> dict[str, int]:
    """Active users per room over the last ``PRESENCE_WINDOW_MINUTES``."""
    try:
        rows = db.all_(
            f"""SELECT room_id, COUNT(*) AS c
                  FROM users
                 WHERE room_id IS NOT NULL
                   AND last_seen >= datetime('now', '-{int(PRESENCE_WINDOW_MINUTES)} minutes')
              GROUP BY room_id""",
        )
    except Exception:  # noqa: BLE001
        return {}
    return {str(r["room_id"]): int(r["c"] or 0) for r in rows}


def _room_dict(row: Any) -> dict:
    return {
        "room_id": str(row["room_id"]),
        "room_udp_host": str(row["room_udp_host"] or "127.0.0.1"),
        "room_udp_port": str(row["room_udp_port"] or "13001"),
        "world_id": str(row["world_id"] or ""),
        "zone_id": str(row["zone_id"] or ""),
    }


def ensure_room_ready(world_id: str, zone_id: str = "") -> dict | None:
    """Pick a room for a joining player, spawning one up if the world is bare.

    Ported from ``join-room/index.php``:

    * Prefer the least-populated room that already has live entities
      (active-user count ASC, then ``room_id`` ASC), so players pile into the
      same room instead of being spread thin.
    * Where the PHP returned ``NO_READY_ROOMS`` on a fresh server, we instead
      spawn entities into the first matching ``room_ids`` row and hand it back.

    Returns ``{"room_id", "room_udp_host", "room_udp_port", "world_id",
    "zone_id"}`` or ``None`` when there is no room row at all.
    """
    _ensure_seed_room()
    candidates = _candidate_rooms(world_id, zone_id)
    if not candidates:
        return None

    ready = [r for r in candidates if room_has_entities(str(r["room_id"]))]
    if ready:
        counts = _presence_counts()
        ready.sort(key=lambda r: (counts.get(str(r["room_id"]), 0),
                                  str(r["room_id"])))
        return _room_dict(ready[0])

    # Nothing has entities yet -> seed the first matching room.
    first = min(candidates, key=lambda r: str(r["room_id"]))
    room = _room_dict(first)
    spawn_room(room["room_id"], room["world_id"], room["zone_id"])
    return room


# --------------------------------------------------------------------------
# top-up -- port of room-spawner.php's main loop, for a single room
# --------------------------------------------------------------------------

def _active_item_ids(pickups: dict[str, dict]) -> list[str]:
    out: list[str] = []
    for payload in pickups.values():
        contents = payload.get("contents")
        if not isinstance(contents, list):
            continue
        for entry in contents:
            if (isinstance(entry, dict) and entry.get("type") == "ITEM"
                    and entry.get("item_id")):
                out.append(str(entry["item_id"]))
    return out


def top_up_room(room_id: str, world_id: str = "", zone_id: str = "") -> dict:
    """Bring an existing room up to ``TARGET_*`` counts (room-spawner.php).

    Unlike :func:`spawn_room` this expects a room that may already have
    entities, uses the spawner's pool (variant always Normal, random bot
    level), and never exceeds the available spawn points.
    """
    room_id = str(room_id)
    rng = _spawn_rng(room_id + ":topup")
    state = get_room_active_state(room_id)
    occupied: set[str] = set(state["occupied_xy"])
    active_pickups: dict[str, dict] = dict(state["pickups"])
    spawned = {"pickups": 0, "wilds": 0, "bots": 0}

    # --- pickups ---
    excludes = _active_item_ids(active_pickups)
    need = max(0, TARGET_PICKUPS_PER_ROOM - len(active_pickups))
    for _ in range(need):
        point = _pick_free_placement(PICKUP_POINTS, occupied, rng)
        if point is None:
            break
        payload = _pickup_payload(point, rng, excludes)
        events.publish(room_id, "room:pickup-added", payload)
        occupied.add(_xy_key(point))
        spawned["pickups"] += 1

    # --- wild Mortys ---
    need = max(0, TARGET_WILD_MORTIES_ROOM - len(state["wilds"]))
    for _ in range(need):
        point = _pick_free_placement(MOB_POINTS, occupied, rng)
        if point is None:
            break
        # Pass None for both so _wild_morty_payload applies the private-server
        # options (all-Shiny) instead of a hardcoded "Normal" overriding them.
        payload = _wild_morty_payload(rng.choice(SPAWNER_WILD_MORTY_POOL),
                                      point, rng, variant=None,
                                      shiny_if_potion=None)
        events.publish(room_id, "room:wild-morty-added", payload)
        occupied.add(_xy_key(point))
        spawned["wilds"] += 1

    # --- bots ---
    need = max(0, TARGET_BOTS_PER_ROOM - len(state["bots"]))
    for _ in range(need):
        point = _pick_free_placement(MOB_POINTS, occupied, rng)
        if point is None:
            break
        payload = _bot_payload(point, rng, rng.randint(BOT_LEVEL_MIN,
                                                       BOT_LEVEL_MAX))
        events.publish(room_id, "room:bot-added", payload)
        occupied.add(_xy_key(point))
        spawned["bots"] += 1

    return spawned
