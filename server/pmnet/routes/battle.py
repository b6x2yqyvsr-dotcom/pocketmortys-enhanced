"""Battle and pickup endpoints.

Ported from the PHP reference:

* ``session/battle-wild-morty/index.php``  -> POST /session/battle-wild-morty
* ``session/battle/ready/index.php``       -> POST /session/battle/ready
* ``session/battle/move/index.php``        -> POST /session/battle/move
* ``session/collect-pickup/index.php``     -> POST /session/collect-pickup
* ``session/battle-bot/index.php``         -> POST /session/battle-bot
* ``session/battle-boss/index.php``        -> POST /session/battle-boss

Battle maths: RECONSTRUCTED, not ported
---------------------------------------
The PHP reference **stubs every battle calculation**.  ``battle/move`` ignores
the request entirely and always emits a hard-coded two-entry ``turn_datas``
array (``AttackTroll`` / ``AttackDoze``) with ``outcome = "RUN"``;
``battle/ready`` only republishes a 30 second timer; ``battle-bot`` and
``battle-boss`` echo ``{"success":true}`` without touching the database at all.
There is therefore no reference formula to port.

This module implements a small, fully deterministic damage model from the
stats that already exist in ``owned_morties`` (``hp_stat`` / ``attack_stat`` /
``defence_stat`` / ``speed_stat``) and ``owned_attacks`` (``power`` / ``amount``
/ ``accuracy``):

    damage = floor( (power * (2*level/5 + 2) * attack / defence) / 50 ) + 2

``element_modifier`` is always ``1`` because no type chart ships in the legacy
database.  Accuracy rolls use a per-battle RNG seeded from
``crc32(battle_id:turn)`` so a replay is reproducible.  Every turn is resolved
server-side and pushed over SSE; the client is a pure renderer.

SSE event names / payload keys
------------------------------
Ported verbatim from PHP (payload key sets match exactly):

* ``battle:start``               PRIVATE  {battle_id, battle_type, player, opponent, meta}
* ``battle:move-timer-started``  PRIVATE  {battle_id, timeout}
* ``battle:turn-result``         PRIVATE  {battle_id, outcome, turn_datas, player_datas}
* ``room:wild-morty-state-changed`` PUBLIC {wild_morty_id, state}
* ``room:user-state-changed``       PUBLIC {player_id, state}
* ``room:pickup-removed``           PUBLIC {pickup_id}
* ``room:pickup-added``             PUBLIC {contents, placement, pickup_id}

The PHP has no capture endpoint, so capture is invented here (the client
clearly needs *some* way to finish a wild encounter).  It reuses PHP idioms --
``insertOwnedMortyWithAttacks`` / ``upsertMortydexCaught`` from
``session/gacha/index.php`` and ``room:wild-morty-removed`` from
``lib/room-spawner.php``:

* ``battle:capture-result``      PRIVATE  {battle_id, wild_morty_id, capture, owned_morty}
"""

from __future__ import annotations

import json
import random
import zlib

from .. import db, events, rooms, state
from ..http import HttpError, error, post

# --------------------------------------------------------------------------
# constants mirrored from the PHP / room-spawner
# --------------------------------------------------------------------------

WILD_OPPONENT_PLAYER_ID = "317D0000-0000-0000-0000-000000000001"
WILD_OPPONENT_NAME = "AWILDMORTY"
WILD_OPPONENT_AVATAR = "NOAVATAR"
WILD_OPPONENT_OWNED_MORTY_ID = "00000000-0000-0000-0000-000000000002"

MAX_ITEM_QUANTITY = 10
MOVE_TIMEOUT_SECONDS = 30

BATTLE_TYPE = {"wild": "PvWM", "bot": "PvB", "boss": "PvBoss"}

# Captured wilds need a moveset: prefer the real promo table, else fall back to
# the four attacks the reference server hands most starter Mortys.
FALLBACK_ATTACKS = [
    ("AttackNail", 10),
    ("AttackStareDown", 10),
    ("AttackDeadStair", 10),
    ("AttackBloodPressure", 10),
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _auth(req) -> dict:
    """Resolve the player, preferring the JWT/session on the request."""
    if req.player:
        return req.player
    body = req.json if isinstance(req.json, dict) else {}
    sid = body.get("session_id") or req.arg("session_id")
    if sid:
        user = db.rowdict(db.one("SELECT * FROM users WHERE session_id=? LIMIT 1", (sid,)))
        if user:
            return user
    raise HttpError(error("NOT_AUTHENTICATED", status=401))


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _body(req) -> dict:
    return req.json if isinstance(req.json, dict) else {}


def _room_id(user: dict) -> str:
    room_id = str(user.get("room_id") or "")
    if room_id in ("", "0"):
        raise HttpError(error("NOT_IN_ROOM", status=409))
    return room_id


def _room_exists(room_id: str) -> bool:
    row = db.one("SELECT 1 AS one FROM room_ids WHERE room_id=? LIMIT 1", (room_id,))
    return row is not None


def _deck_order(user: dict) -> list[str]:
    deck_id = user.get("active_deck_id")
    try:
        deck_id = int(deck_id)
    except (TypeError, ValueError):
        return []
    if not deck_id:
        return []
    row = db.one("SELECT owned_morty_ids FROM decks WHERE deck_id=? LIMIT 1", (deck_id,))
    if not row:
        return []
    return state.decode_ids(row["owned_morty_ids"])


# --------------------------------------------------------------------------
# battle persistence
# --------------------------------------------------------------------------

def _load_battle(battle_id: str) -> dict | None:
    if not battle_id:
        return None
    row = db.one("SELECT * FROM battles WHERE battle_id=? LIMIT 1", (battle_id,))
    if not row:
        return None
    battle = dict(row)
    try:
        battle["state"] = json.loads(battle["state"])
    except (TypeError, ValueError):
        battle["state"] = {}
    return battle


def _create_battle(kind: str, room_id: str, player_id: str, st: dict) -> str:
    battle_id = str(st.get("battle_id") or events.new_id())
    st["battle_id"] = battle_id
    stamp = db.now()
    db.run(
        """INSERT INTO battles
               (battle_id, room_id, player_id, kind, state, created_at, updated_at, finished)
           VALUES (?,?,?,?,?,?,?,0)""",
        (battle_id, room_id, player_id, kind,
         json.dumps(st, separators=(",", ":"), ensure_ascii=False), stamp, stamp),
    )
    return battle_id


def _save_battle(battle_id: str, st: dict, finished: bool = False) -> None:
    db.run(
        "UPDATE battles SET state=?, updated_at=?, finished=? WHERE battle_id=?",
        (json.dumps(st, separators=(",", ":"), ensure_ascii=False),
         db.now(), 1 if finished else 0, battle_id),
    )


# --------------------------------------------------------------------------
# player / opponent payload blocks
# --------------------------------------------------------------------------

def _default_move_log() -> dict:
    # PHP uses stdClass for the empty maps -- they serialise as {}.
    return {
        "cooldown": {},
        "count": {},
        "cooldown_next": {"ITEM": 1},
        "last_move_type": {},
    }


def _player_block(user: dict, session_id: str) -> dict:
    player_id = str(user["player_id"])
    morties = state.owned_morties(player_id, _deck_order(user))

    active = ""
    for morty in morties:
        if int(morty.get("hp") or 0) > 0:
            active = morty["owned_morty_id"]
            break

    return {
        "player_id": player_id,
        "username": str(user.get("username") or ""),
        "player_avatar_id": str(user.get("player_avatar_id") or ""),
        "level": int(user.get("level") or 0),
        "xp": int(user.get("xp") or 0),
        "streak": int(user.get("streak") or 0),
        "coins": int(user.get("coins") or 0),
        "coupons": int(user.get("coupons") or 0),
        "permits": int(user.get("permits") or 0),
        "owned_morties": morties,
        "owned_items": state.owned_items(player_id),
        "tags": [],
        "xp_lower": int(user.get("xp_lower") or 0),
        "xp_upper": int(user.get("xp_upper") or 0),
        "_meta": {
            "session_id": str(session_id or ""),
            "isPlayerInDB": True,
            "isControlledByAI": False,
            "isRaidBoss": False,
        },
        "active_owned_morty": active,
        "move_log": _default_move_log(),
    }


def _find_room_payload(room_id: str, event_name: str, id_key: str,
                       id_value: str, limit: int = 50) -> dict | None:
    rows = db.all_(
        """SELECT payload_json FROM event_queue
            WHERE room_id=? AND event_name=?
            ORDER BY id DESC LIMIT ?""",
        (room_id, event_name, limit),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get(id_key) or "") == id_value:
            return payload
    return None


def _wild_opponent(wild: dict | None) -> dict:
    """PHP's hard-coded wild opponent, with stats reconstructed from division."""
    wild = wild or {}
    morty_id = str(wild.get("morty_id") or "MortyDefault")
    try:
        division = int(wild.get("division") or 1)
    except (TypeError, ValueError):
        division = 1
    division = max(1, division)

    level = 5 + (division - 1) * 4
    hp = 20 + level * 3
    atk = 10 + level * 2
    dfn = 10 + level * 2
    spd = 10 + level * 2

    return {
        "player_id": WILD_OPPONENT_PLAYER_ID,
        "username": WILD_OPPONENT_NAME,
        "player_avatar_id": WILD_OPPONENT_AVATAR,
        "owned_morties": [{
            "owned_morty_id": WILD_OPPONENT_OWNED_MORTY_ID,
            "morty_id": morty_id,
            "level": level,
            "xp": int(level * level * 28),
            "hp": hp,
            "hp_stat": hp,
            "attack_stat": atk,
            "defence_stat": dfn,
            "speed_stat": spd,
            "variant": str(wild.get("variant") or "Normal"),
        }],
        "streak": 0,
        "shiny_if_potion": bool(wild.get("shiny_if_potion", False)),
        "_meta": {
            "isPlayerInDB": False,
            "isControlledByAI": True,
            "isRaidBoss": False,
        },
        "active_owned_morty": WILD_OPPONENT_OWNED_MORTY_ID,
    }


def _bot_opponent(user: dict, bot: dict | None) -> dict:
    bot = bot or {}
    morties = bot.get("owned_morties")
    if not isinstance(morties, list) or not morties:
        level = int(bot.get("level") or 5)
        morties = [{
            "owned_morty_id": rooms.BOT_OWNED_MORTY_ID,
            "morty_id": "MortyDefault",
            "level": level,
            "xp": int(level * level * 28),
            "hp": 20 + level * 3,
            "hp_stat": 20 + level * 3,
            "attack_stat": 10 + level * 2,
            "defence_stat": 10 + level * 2,
            "speed_stat": 10 + level * 2,
            "variant": "Normal",
        }]
    first = morties[0]
    return {
        "player_id": str(bot.get("bot_id") or bot.get("player_id") or WILD_OPPONENT_PLAYER_ID),
        "username": str(bot.get("username") or "BOT"),
        "player_avatar_id": str(bot.get("player_avatar_id") or WILD_OPPONENT_AVATAR),
        "owned_morties": morties,
        "streak": int(bot.get("streak") or 0),
        "shiny_if_potion": False,
        "_meta": {
            "isPlayerInDB": False,
            "isControlledByAI": True,
            "isRaidBoss": False,
        },
        "active_owned_morty": str(first.get("owned_morty_id") or ""),
    }


# --------------------------------------------------------------------------
# deterministic battle maths (component B -- see module docstring)
# --------------------------------------------------------------------------

def _stat(entity: dict, key: str) -> int:
    value = entity.get(key)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        # Fall back to hp_stat/2 so an un-seeded morty can still fight.
        try:
            return max(1, int(entity.get("hp_stat") or 0) // 2)
        except (TypeError, ValueError):
            return 1
    return value


def _move_power(attack: dict) -> int:
    """Power of a move, preferring the game's own AttackInfo table.

    The ``owned_attacks`` columns ``power``/``amount`` are NULL for almost
    every row -- the reference server never populated them -- which is why
    battle damage used to be computed from a hardcoded default.  The real
    per-effect power now comes from the client's AttackInfo, which is
    authoritative; the stored columns are only a fallback.
    """
    attack_id = str(attack.get("attack_id") or attack.get("id") or "")
    if attack_id:
        try:
            from .. import gamedata
            if attack_id in gamedata.attacks():
                # The table is authoritative, including the zero: a status
                # move really does no direct damage, and clamping it up to the
                # generic fallback would make e.g. AttackStareDown hit as hard
                # as a mid-tier attack.
                return gamedata.attack_power(attack_id)
        except Exception:  # noqa: BLE001
            pass

    for key in ("power", "amount"):
        try:
            value = int(attack.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    # Unknown move id: keep the damage pipeline total rather than dealing 0.
    return 40


def _damage(attacker: dict, defender: dict, attack: dict) -> int:
    """Damage using the client's own formula (see gamedata.damage).

    This replaces a reconstruction based on a Pokemon-shaped guess.  The real
    routine was read out of ``MortyDefs.ApplyDamage``, and the type chart out
    of ``GetElementTypeModifier`` -- which turns out to be rock-paper-scissors.
    """
    power = _move_power(attack)
    level = max(1, int(attacker.get("level") or 1))
    atk = _stat(attacker, "attack_stat")
    dfn = _stat(defender, "defence_stat")

    try:
        from .. import gamedata
        attack_id = str(attack.get("attack_id") or attack.get("id") or "")
        type_mod = gamedata.element_modifier(
            gamedata.attack_element(attack_id),
            gamedata.morty_element(str(defender.get("morty_id") or "")),
        )
        result = gamedata.damage(
            attack_stat=atk,
            defence_stat=dfn,
            level=level,
            power=power,
            type_modifier=type_mod,
            defender_level=int(defender.get("level") or level),
        )
    except Exception:  # noqa: BLE001
        # Never let a missing data table break a battle outright.
        result = int((power * (2 * level / 5 + 2) * atk / max(dfn, 1)) / 50 + 2)

    return max(1, result)


def _hit_effect(attacker: dict, defender: dict, attack: dict,
                rng: random.Random) -> tuple[dict, int]:
    """Return ``(effect_datas, damage)`` for one attacking move."""
    accuracy = attack.get("accuracy")
    try:
        accuracy = int(accuracy)
    except (TypeError, ValueError):
        accuracy = 100 if _truthy(attack.get("is_accurate", 1)) else 75
    if accuracy <= 0:
        accuracy = 100

    effects: list[dict] = []
    if rng.randint(1, 100) > accuracy:
        effects.append({
            "type": "Miss",
            "is_accurate": False,
            "to_self": False,
        })
        return effects, 0

    damage = _damage(attacker, defender, attack)
    critical = rng.randint(1, 100) <= 6
    if critical:
        damage = int(damage * 1.5)

    stat = attack.get("stat")
    amount = attack.get("amount")
    if stat and amount not in (None, 0):
        effects.append({
            "type": "Stat",
            "is_accurate": True,
            "to_self": _truthy(attack.get("to_self")),
            "stat": str(stat),
            "amount": int(amount),
        })

    defender_morty_datas = [{
        "owned_morty_id": str(defender.get("owned_morty_id") or ""),
        "hp": max(0, int(defender.get("hp") or 0) - damage),
    }]
    effects.append({
        "type": "Hit",
        "is_accurate": True,
        "to_self": False,
        "is_critical": bool(critical),
        "damage": damage,
        "defender_morty_datas": defender_morty_datas,
    })
    return effects, damage


def _turn_entry(attacker_player: str, defender_player: str, attack: dict,
                attacker_morty: dict, defender_morty: dict,
                rng: random.Random) -> tuple[dict, int]:
    effects, damage = _hit_effect(attacker_morty, defender_morty, attack, rng)
    return {
        "type": "ATTACK",
        "attacker_player_id": attacker_player,
        "defender_player_id": defender_player,
        "attack_id": str(attack.get("attack_id") or ""),
        "element_modifier": 1,
        "effect_datas": effects,
        "attacker_morty_datas": [{
            "owned_morty_id": str(attacker_morty.get("owned_morty_id") or ""),
            "owned_attacks": [{
                "attack_id": str(attack.get("attack_id") or ""),
                "pp": max(0, int(attack.get("pp") or 0) - 1),
            }],
        }],
    }, damage


def _opponent_moveset(opponent: dict) -> list[dict]:
    morty = (opponent.get("owned_morties") or [{}])[0]
    moves = morty.get("owned_attacks")
    if isinstance(moves, list) and moves:
        return moves
    return [{"attack_id": "AttackDoze", "pp": 9, "power": 40, "accuracy": 100}]


# --------------------------------------------------------------------------
# capture (invented -- see docstring)
# --------------------------------------------------------------------------

def _capture_moveset(morty_id: str) -> list[dict]:
    rows = db.all_(
        """SELECT position, attack_id, pp, pp_stat
             FROM gacha_promo_morty_attacks
            WHERE morty_id=? ORDER BY position ASC""",
        (morty_id,),
    )
    if rows:
        return [
            {"position": int(r["position"] or i), "attack_id": str(r["attack_id"]),
             "pp": int(r["pp"] or 10), "pp_stat": int(r["pp_stat"] or 10)}
            for i, r in enumerate(rows)
        ]
    return [
        {"position": i, "attack_id": attack_id, "pp": pp, "pp_stat": pp}
        for i, (attack_id, pp) in enumerate(FALLBACK_ATTACKS)
    ]


def _upsert_mortydex_caught(player_id: str, morty_id: str) -> None:
    row = db.one(
        "SELECT id FROM mortydex WHERE player_id=? AND morty_id=? LIMIT 1",
        (player_id, morty_id),
    )
    if row:
        db.run("UPDATE mortydex SET caught='true' WHERE id=?", (int(row["id"]),))
    else:
        db.run(
            "INSERT INTO mortydex (player_id, morty_id, caught) VALUES (?,?,'true')",
            (player_id, morty_id),
        )


def _insert_owned_morty(player_id: str, wild: dict) -> dict:
    """Port of gacha's ``insertOwnedMortyWithAttacks`` for a caught wild."""
    morty_id = str(wild.get("morty_id") or "MortyDefault")
    variant = str(wild.get("variant") or "Normal")
    try:
        division = int(wild.get("division") or 1)
    except (TypeError, ValueError):
        division = 1
    division = max(1, division)

    level = 5 + (division - 1) * 4
    hp = 20 + level * 3
    atk = 10 + level * 2
    dfn = 10 + level * 2
    spd = 10 + level * 2
    xp = int(round((level * level) * 28.0))
    owned_morty_id = events.new_id()

    db.run(
        """INSERT INTO owned_morties
               (player_id, owned_morty_id, morty_id, level, xp, hp, hp_stat,
                attack_stat, defence_stat, variant, speed_stat, is_locked,
                is_trading_locked, fight_pit_id, evolution_points, xp_lower, xp_upper)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'false','false','null',0,?,?)""",
        (player_id, owned_morty_id, morty_id, level, xp, hp, hp, atk, dfn,
         variant, spd, xp, xp),
    )

    attacks_out = []
    for attack in _capture_moveset(morty_id):
        db.run(
            """INSERT INTO owned_attacks
                   (owned_morty_id, position, attack_id, pp, pp_stat,
                    type, is_accurate, to_self, stat, amount)
               VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL,NULL)""",
            (owned_morty_id, int(attack["position"]), str(attack["attack_id"]),
             int(attack["pp"]), int(attack["pp_stat"])),
        )
        attacks_out.append({
            "attack_id": str(attack["attack_id"]),
            "pp": int(attack["pp"]),
            "position": int(attack["position"]),
        })

    _upsert_mortydex_caught(player_id, morty_id)

    return {
        "owned_morty_id": owned_morty_id,
        "player_id": player_id,
        "morty_id": morty_id,
        "level": level,
        "xp": xp,
        "hp": hp,
        "hp_stat": hp,
        "attack_stat": atk,
        "defence_stat": dfn,
        "speed_stat": spd,
        "variant": variant,
        "owned_attacks": attacks_out,
    }


def _remove_wild(room_id: str, player_id: str, wild_morty_id: str) -> None:
    """Drop the wild Morty from the room the way room-spawner expects."""
    events.publish(room_id, "room:wild-morty-removed",
                   {"wild_morty_id": str(wild_morty_id)})
    events.publish(room_id, "room:user-state-changed",
                   {"player_id": str(player_id), "state": "WORLD"})


# --------------------------------------------------------------------------
# battle start endpoints
# --------------------------------------------------------------------------

@post("/session/battle-wild-morty")
def battle_wild_morty(req):
    """Start (or capture during) a wild encounter -- PHP battle-wild-morty."""
    body = _body(req)
    user = _auth(req)
    player_id = str(user["player_id"])
    room_id = _room_id(user)
    if not _room_exists(room_id):
        raise HttpError(error("ROOM_NOT_FOUND", status=409, room_id=room_id))

    wild_morty_id = str(body.get("wild_morty_id") or "")
    if not wild_morty_id:
        raise HttpError(error("MISSING_WILD_MORTY_ID", status=400))

    wild = _find_room_payload(room_id, "room:wild-morty-added",
                              "wild_morty_id", wild_morty_id)
    active = rooms.get_room_active_state(room_id)["wilds"]
    still_active = wild_morty_id in active

    action = str(body.get("action") or "").lower()
    wants_capture = action == "capture" or _truthy(body.get("capture"))

    # ---- capture branch (invented; PHP has no capture endpoint) ----------
    if wants_capture:
        if wild is None or not still_active:
            raise HttpError(error("WILD_MORTY_NOT_FOUND", status=404,
                                  wild_morty_id=wild_morty_id))

        owned_morty = _insert_owned_morty(player_id, wild)
        _remove_wild(room_id, player_id, wild_morty_id)

        battle_id = str(body.get("battle_id") or "")
        _finish_battle(battle_id, player_id, "WIN")

        events.publish(room_id, "battle:capture-result", {
            "battle_id": battle_id,
            "wild_morty_id": wild_morty_id,
            "capture": True,
            "owned_morty": owned_morty,
        }, player_id=player_id)

        return {"success": True, "capture": True,
                "owned_morty_id": owned_morty["owned_morty_id"],
                "morty_id": owned_morty["morty_id"]}

    # ---- normal start (mirrors PHP exactly) ------------------------------
    session_id = str(body.get("session_id") or "")
    player = _player_block(user, session_id)
    opponent = _wild_opponent(wild)
    st = {
        "battle_id": events.new_id(),
        "kind": "wild",
        "battle_type": BATTLE_TYPE["wild"],
        "room_id": room_id,
        "player_id": player_id,
        "turn": 0,
        "status": "active",
        "wild_morty_id": wild_morty_id,
        "opponent": opponent,
        "player_hp": {},
    }
    battle_id = _create_battle("wild", room_id, player_id, st)
    st["battle_id"] = battle_id
    _save_battle(battle_id, st)

    payload = {
        "battle_id": battle_id,
        "battle_type": BATTLE_TYPE["wild"],
        "player": player,
        "opponent": opponent,
        "meta": {},
    }
    events.publish(room_id, "battle:start", payload, player_id=player_id)
    events.publish(room_id, "room:wild-morty-state-changed",
                   {"wild_morty_id": wild_morty_id, "state": "BATTLE"})
    events.publish(room_id, "room:user-state-changed",
                   {"player_id": player_id, "state": "BATTLE"})

    return {"success": True}


@post("/session/battle-bot")
def battle_bot(req):
    """Start a bot battle.

    The PHP endpoint is a bare ``{"success":true}`` stub (it publishes
    nothing), so the event shape here is reconstructed from ``battle:start``.
    """
    return _start_ai_battle(req, "bot")


@post("/session/battle-boss")
def battle_boss(req):
    """Start a boss battle -- PHP is a bare ``{"success":true}`` stub."""
    return _start_ai_battle(req, "boss")


def _start_ai_battle(req, kind: str):
    body = _body(req)
    user = _auth(req)
    player_id = str(user["player_id"])
    room_id = _room_id(user)

    opponent: dict
    if kind == "bot":
        bot_id = str(body.get("bot_id") or "")
        bot = None
        if bot_id:
            bot = _find_room_payload(room_id, "room:bot-added", "bot_id", bot_id)
        opponent = _bot_opponent(user, bot)
    else:
        boss = body.get("boss") if isinstance(body.get("boss"), dict) else None
        opponent = _bot_opponent(user, boss)

    session_id = str(body.get("session_id") or "")
    player = _player_block(user, session_id)
    st = {
        "battle_id": events.new_id(),
        "kind": kind,
        "battle_type": BATTLE_TYPE.get(kind, "PvB"),
        "room_id": room_id,
        "player_id": player_id,
        "turn": 0,
        "status": "active",
        "opponent": opponent,
        "player_hp": {},
    }
    battle_id = _create_battle(kind, room_id, player_id, st)
    st["battle_id"] = battle_id
    _save_battle(battle_id, st)

    events.publish(room_id, "battle:start", {
        "battle_id": battle_id,
        "battle_type": BATTLE_TYPE.get(kind, "PvB"),
        "player": player,
        "opponent": opponent,
        "meta": {},
    }, player_id=player_id)
    events.publish(room_id, "room:user-state-changed",
                   {"player_id": player_id, "state": "BATTLE"})
    return {"success": True}


# --------------------------------------------------------------------------
# ready / move
# --------------------------------------------------------------------------

@post("/session/battle/ready")
def battle_ready(req):
    """PHP battle/ready: republish the per-turn move timer (30s)."""
    body = _body(req)
    user = _auth(req)
    player_id = str(user["player_id"])
    room_id = _room_id(user)

    battle_id = str(body.get("battle_id") or body.get("session_id") or "")
    battle = _load_battle(battle_id)
    if battle and not battle["finished"]:
        st = battle["state"]
        st["timer_started_at"] = db.iso_now()
        _save_battle(battle_id, st)

    events.publish(room_id, "battle:move-timer-started",
                   {"battle_id": battle_id, "timeout": MOVE_TIMEOUT_SECONDS},
                   player_id=player_id)
    return {"success": True}


def _finish_battle(battle_id: str, player_id: str, outcome: str) -> None:
    battle = _load_battle(battle_id)
    if not battle:
        return
    st = battle["state"]
    st["status"] = "finished"
    st["outcome"] = outcome
    _save_battle(battle_id, st, finished=True)


@post("/session/battle/move")
def battle_move(req):
    """Resolve one turn server-side and push ``battle:turn-result``."""
    body = _body(req)
    user = _auth(req)
    player_id = str(user["player_id"])
    room_id = _room_id(user)

    battle_id = str(body.get("battle_id") or "")
    owned_morty_id = str(body.get("owned_morty_id") or "")
    move_id = body.get("move_id")
    if move_id is not None:
        move_id = str(move_id).strip() or None
    move_type = str(body.get("type") or "").upper()

    if not battle_id:
        raise HttpError(error("MISSING_BATTLE_ID", status=400))
    if not owned_morty_id:
        raise HttpError(error("MISSING_OWNED_MORTY_ID", status=400))

    own = db.one(
        """SELECT owned_morty_id, hp FROM owned_morties
            WHERE owned_morty_id=? AND player_id=? LIMIT 1""",
        (owned_morty_id, player_id),
    )
    if not own:
        raise HttpError(error("MORTY_NOT_OWNED", status=403,
                              owned_morty_id=owned_morty_id))
    if int(own["hp"] or 0) <= 0:
        raise HttpError(error("MORTY_NO_HP", status=409,
                              owned_morty_id=owned_morty_id))

    attack: dict | None = None
    if move_id:
        row = db.one(
            """SELECT attack_id, position, pp, pp_stat, amount, power, accuracy,
                      stat, type, to_self, is_accurate
                 FROM owned_attacks
                WHERE owned_morty_id=? AND attack_id=? LIMIT 1""",
            (owned_morty_id, move_id),
        )
        if not row:
            raise HttpError(error("ATTACK_NOT_OWNED", status=400,
                                  attack_id=move_id))
        if int(row["pp"] or 0) <= 0:
            raise HttpError(error("NO_PP", status=409, attack_id=move_id))
        attack = dict(row)

    battle = _load_battle(battle_id)
    st = battle["state"] if battle else {}
    opponent = st.get("opponent") or _wild_opponent(None)

    player_block = _player_block(user, str(body.get("session_id") or ""))
    attacker_morty = _find_morty(player_block["owned_morties"], owned_morty_id)
    defender_morty = dict((opponent.get("owned_morties") or [{}])[0])

    rng = random.Random(zlib.crc32(f"{battle_id}:{st.get('turn', 0)}".encode()))
    turns: list[dict] = []
    outcome = "CONTINUE"

    if move_type == "RUN":
        outcome = "RUN"
    elif move_type == "ITEM":
        turns.append({
            "type": "ITEM",
            "attacker_player_id": player_id,
            "defender_player_id": str(opponent.get("player_id") or ""),
            "item_id": move_id,
            "effect_datas": [],
            "attacker_morty_datas": [{
                "owned_morty_id": owned_morty_id,
                "owned_attacks": [],
            }],
        })
    elif attack is not None:
        entry, damage = _turn_entry(
            player_id, str(opponent.get("player_id") or ""), attack,
            attacker_morty, defender_morty, rng)
        turns.append(entry)
        if damage:
            defender_morty["hp"] = max(0, int(defender_morty.get("hp") or 0) - damage)
        db.run(
            "UPDATE owned_attacks SET pp=MAX(0, pp-1) WHERE owned_morty_id=? AND attack_id=?",
            (owned_morty_id, move_id),
        )
        if int(defender_morty.get("hp") or 0) <= 0:
            outcome = "WIN"
        else:
            opp_attack = _opponent_moveset(opponent)[0]
            opp_entry, opp_damage = _turn_entry(
                str(opponent.get("player_id") or ""), player_id, opp_attack,
                defender_morty, attacker_morty, rng)
            turns.append(opp_entry)
            new_hp = max(0, int(attacker_morty.get("hp") or 0) - opp_damage)
            attacker_morty["hp"] = new_hp
            db.run("UPDATE owned_morties SET hp=? WHERE owned_morty_id=?",
                   (new_hp, owned_morty_id))
            if new_hp <= 0:
                outcome = "LOSE"

    if battle:
        st["turn"] = int(st.get("turn") or 0) + 1
        opp_first = (opponent.get("owned_morties") or [{}])[0]
        if opp_first:
            opp_first["hp"] = int(defender_morty.get("hp") or 0)
        st["opponent"] = opponent
        if outcome in ("WIN", "LOSE", "RUN"):
            st["status"] = "finished"
            st["outcome"] = outcome
        _save_battle(battle_id, st, finished=outcome in ("WIN", "LOSE", "RUN"))

    payload = {
        "battle_id": battle_id,
        "outcome": outcome,
        "turn_datas": turns,
        "player_datas": {
            "player": {"move_log": {
                "cooldown": {move_type or "ATTACK": 0},
                "count": {move_type or "ATTACK": 1},
                "cooldown_next": {"ITEM": 1},
                "last_move_type": move_type or "ATTACK",
            }},
            "opponent": {},
        },
    }
    events.publish(room_id, "battle:turn-result", payload, player_id=player_id)

    if outcome in ("WIN", "LOSE", "RUN"):
        events.publish(room_id, "room:user-state-changed",
                       {"player_id": player_id, "state": "WORLD"})

    return {"success": True}


def _find_morty(morties: list[dict], owned_morty_id: str) -> dict:
    for morty in morties:
        if str(morty.get("owned_morty_id")) == owned_morty_id:
            return morty
    return {"owned_morty_id": owned_morty_id, "hp": 1, "hp_stat": 1,
            "level": 1, "attack_stat": 1, "defence_stat": 1, "speed_stat": 1}


# --------------------------------------------------------------------------
# collect-pickup
# --------------------------------------------------------------------------

def _grant_item(player_id: str, item_id: str, add_qty: int) -> dict:
    row = db.one(
        "SELECT id, quantity FROM owned_items WHERE player_id=? AND item_id=? LIMIT 1",
        (player_id, item_id),
    )
    current = int(row["quantity"]) if row else 0

    if current >= MAX_ITEM_QUANTITY or add_qty <= 0:
        return {"type": "ITEM", "item_id": item_id, "quantity": current,
                "amount_received": 0, "amount": 1}

    target = min(MAX_ITEM_QUANTITY, current + add_qty)
    added = max(0, target - current)
    if row:
        if added > 0:
            db.run("UPDATE owned_items SET quantity=? WHERE id=?", (target, int(row["id"])))
    elif added > 0:
        db.run("INSERT INTO owned_items (player_id, item_id, quantity) VALUES (?,?,?)",
               (player_id, item_id, target))

    return {"type": "ITEM", "item_id": item_id, "quantity": target,
            "amount_received": added, "amount": 1}


def _grant_coins(player_id: str, add: int) -> dict:
    if add <= 0:
        row = db.one("SELECT coins FROM users WHERE player_id=? LIMIT 1", (player_id,))
        return {"type": "COIN", "quantity": int(row["coins"] or 0) if row else 0,
                "amount_received": 0, "amount": 0}

    db.run("UPDATE users SET coins = COALESCE(coins,0) + ? WHERE player_id=?",
           (add, player_id))
    row = db.one("SELECT coins FROM users WHERE player_id=? LIMIT 1", (player_id,))
    new_total = int(row["coins"] or 0) if row else add
    return {"type": "COIN", "quantity": new_total,
            "amount_received": add, "amount": add}


@post("/session/collect-pickup")
def collect_pickup(req):
    """Port of session/collect-pickup: grant loot and respawn one pickup."""
    body = _body(req)
    user = _auth(req)
    player_id = str(user["player_id"])
    room_id = _room_id(user)
    pickup_id = str(body.get("pickup_id") or "")
    if not pickup_id:
        raise HttpError(error("MISSING_PICKUP_ID", status=400))

    spawn = db.one(
        """SELECT id, payload_json, pickup_id, pickup_id_collected_by_player_id
             FROM event_queue
            WHERE room_id=? AND event_name='room:pickup-added' AND pickup_id=?
            ORDER BY id DESC LIMIT 1""",
        (room_id, pickup_id),
    )
    if not spawn:
        raise HttpError(error("PICKUP_NOT_FOUND", status=404))

    collected_by = spawn["pickup_id_collected_by_player_id"]
    if collected_by:
        raise HttpError(error("PICKUP_ALREADY_COLLECTED", status=409))

    try:
        payload = json.loads(spawn["payload_json"])
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("contents"), list):
        raise HttpError(error("PICKUP_MALFORMED", status=500))

    exclude: list[str] = []
    out_items: list[dict] = []
    out_coins: list[dict] = []
    for content in payload["contents"]:
        if not isinstance(content, dict):
            continue
        kind = str(content.get("type") or "")
        if kind == "ITEM":
            item_id = str(content.get("item_id") or "")
            if not item_id:
                continue
            exclude.append(item_id)
            out_items.append(_grant_item(player_id, item_id, int(content.get("amount") or 1)))
        elif kind == "COIN":
            out_coins.append(_grant_coins(player_id, int(content.get("amount") or 0)))

    # Mark the spawn row collected (the snapshot replayer honours this column)
    # and tell the room it is gone.
    db.run(
        "UPDATE event_queue SET pickup_id_collected_by_player_id=? WHERE id=?",
        (player_id, int(spawn["id"])),
    )
    events.publish(room_id, "room:pickup-removed", {"pickup_id": pickup_id})

    # Spawn exactly one replacement, avoiding the items just picked up.
    active = rooms.get_room_active_state(room_id)
    rng = rooms._spawn_rng(f"{room_id}:collect:{pickup_id}")
    point = rooms._pick_free_placement(rooms.PICKUP_POINTS, active["occupied_xy"], rng)
    if point is None:
        point = list(rng.choice(rooms.PICKUP_POINTS))
    new_pickup = rooms._pickup_payload(point, rng, exclude)
    events.publish(room_id, "room:pickup-added", new_pickup)

    return {
        "pickup_id": pickup_id,
        "contents": out_items + out_coins,
    }
