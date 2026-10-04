"""Core session endpoints.

This module covers the traffic the client emits immediately after connecting:
state updates, the full player blob, room entry/exit and deck management.
Battle, gacha, arena, event, friend and trading endpoints live in their own
modules.
"""

from __future__ import annotations

import json

from .. import config, db, events, state
from ..http import HttpError, error, get, post, require_player


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


# --------------------------------------------------------------------------
# trivial state endpoints
# --------------------------------------------------------------------------

@post("/session/state")
def session_state(req):
    """The client tells us what state it thinks it is in; we echo it.

    The reference does exactly this.  It exists so the server can track
    WORLD / BATTLE / MENU transitions for room presence.
    """
    body = req.json or {}
    new_state = str(body.get("state") or "WORLD")
    if req.player:
        db.run("UPDATE users SET state=?, last_seen=CURRENT_TIMESTAMP WHERE player_id=?",
               (new_state, req.player["player_id"]))
    return {"state": new_state}


@post("/session/player-details")
def player_details(req):
    return state.player_details(_auth(req))


@get("/session/player-details")
def player_details_get(req):
    return state.player_details(_auth(req))


# --------------------------------------------------------------------------
# config endpoints
# --------------------------------------------------------------------------

@get("/decks/slots/config")
@post("/decks/slots/config")
def deck_slots_config(req):
    row = db.one("SELECT * FROM deck_config WHERE config_id='MP' LIMIT 1")
    if not row:
        return {"starting_deck_slots": 3, "max_deck_slots": 9,
                "cost_additional_slot": 10}
    return {
        "starting_deck_slots": int(row["starting_deck_slots"]),
        "max_deck_slots": int(row["max_deck_slots"]),
        "cost_additional_slot": int(row["cost_additional_slot"]),
    }


# NOTE: /morty/slots/config and /trading/config are deliberately NOT defined
# here.  Both exist in the PHP reference with a different response shape than
# the obvious one, and they are owned by the modules that ported them
# faithfully (routes/misc.py and routes/social.py respectively).  Registering
# them here as well caused the later module to silently shadow this one.


# --------------------------------------------------------------------------
# rooms
# --------------------------------------------------------------------------

@post("/session/join-room")
def join_room(req):
    user = _auth(req)
    body = req.json or {}
    world_id = str(body.get("world_id") or "1")

    from .. import rooms as rooms_mod

    room = rooms_mod.ensure_room_ready(world_id)
    if not room:
        raise HttpError(error("NO_READY_ROOMS", status=400,
                              detail="No rooms currently available."))

    room_id = str(room["room_id"])
    player_id = str(user["player_id"])

    db.run("""UPDATE users
                 SET room_id=?, state='WORLD', last_seen=CURRENT_TIMESTAMP
               WHERE player_id=?""", (room_id, player_id))

    baseline = events.room_max_id(room_id)
    db.run("UPDATE users SET last_event_id=? WHERE player_id=?",
           (baseline, player_id))

    snapshot = rooms_mod.build_room_snapshot(room_id)

    # announce ourselves to the room
    events.publish(room_id, "room:user-added", {
        "player_id": player_id,
        "username": str(user["username"] or ""),
        "player_avatar_id": str(user["player_avatar_id"] or "AvatarRickDefault"),
        "level": int(user["level"] or 1),
        "owned_morties": _deck_morties(player_id, user),
        "state": "WORLD",
    })

    return {
        "room_id": room_id,
        "room_udp_host": str(room.get("room_udp_host") or "127.0.0.1"),
        "room_udp_port": str(room.get("room_udp_port") or "13001"),
        "world_id": str(room.get("world_id") or world_id),
        "zone_id": str(room.get("zone_id") or "[1-5]"),
        "incentive": _incentive(),
        "users": _room_users(room_id, player_id),
        "pickups": snapshot.get("pickups", []),
        "wild_morties": snapshot.get("wild_morties", []),
        "bots": snapshot.get("bots", []),
        "baseline_event_id": baseline,
    }


def _incentive() -> dict:
    return {
        "incentive_id": "NPCAd",
        "rewards": [
            {"type": "ITEM", "amount": 1, "item_id": "ItemSerum", "rarity": 100},
            {"type": "ITEM", "amount": 1, "item_id": "ItemParalysisCure", "rarity": 75},
            {"type": "COIN", "amount": 200},
        ],
        "token": "",
    }


def _deck_morties(player_id: str, user: dict) -> list[dict]:
    """Active-deck Mortys in the compact shape join-room uses."""
    active = int(user.get("active_deck_id") or 0)
    deck = db.one("SELECT owned_morty_ids FROM decks WHERE player_id=? AND deck_id=?",
                  (player_id, active))
    ids = state.decode_ids(deck["owned_morty_ids"]) if deck else []
    if not ids:
        return []

    holes = ",".join("?" * len(ids))
    rows = db.all_(
        f"""SELECT owned_morty_id, morty_id, hp, variant, is_locked,
                   is_trading_locked, fight_pit_id
              FROM owned_morties WHERE owned_morty_id IN ({holes})
             ORDER BY id ASC""",
        ids,
    )
    by_id = {str(r["owned_morty_id"]): r for r in rows}

    out = []
    for oid in ids:
        row = by_id.get(oid)
        if not row:
            continue
        out.append({
            "owned_morty_id": oid,
            "morty_id": str(row["morty_id"]),
            "hp": int(row["hp"] or 0),
            "variant": str(row["variant"] or "Normal"),
            "is_locked": state.truthy(row["is_locked"]),
            "is_trading_locked": state.truthy(row["is_trading_locked"]),
            "fight_pit_id": state.maybe_null(row["fight_pit_id"]),
        })
    return out


def _room_users(room_id: str, exclude_player: str | None = None) -> list[dict]:
    stale = db.iso_now()
    rows = db.all_(
        """SELECT player_id, username, player_avatar_id, level, state, active_deck_id
             FROM users WHERE room_id=? ORDER BY last_seen DESC""",
        (room_id,),
    )
    out = []
    for row in rows:
        pid = str(row["player_id"])
        if exclude_player and pid == exclude_player:
            continue
        out.append({
            "player_id": pid,
            "username": str(row["username"] or ""),
            "player_avatar_id": str(row["player_avatar_id"] or "AvatarRickDefault"),
            "level": int(row["level"] or 1),
            "owned_morties": _deck_morties(pid, dict(row)),
            "state": str(row["state"] or "WORLD"),
        })
    return out


@post("/session/leave-room")
def leave_room(req):
    user = _auth(req)
    room_id = str(user.get("room_id") or "")
    db.run("UPDATE users SET room_id=NULL, state='MENU' WHERE player_id=?",
           (user["player_id"],))
    if room_id:
        events.publish(room_id, "room:user-removed",
                       {"player_id": str(user["player_id"])})
    return {"success": True}


@post("/session/room-details")
def room_details(req):
    user = _auth(req)
    room_id = str(user.get("room_id") or "")
    from .. import rooms as rooms_mod
    snapshot = rooms_mod.build_room_snapshot(room_id) if room_id else {}
    return {
        "room_id": room_id,
        "users": _room_users(room_id, str(user["player_id"])),
        "pickups": snapshot.get("pickups", []),
        "wild_morties": snapshot.get("wild_morties", []),
        "bots": snapshot.get("bots", []),
    }


# --------------------------------------------------------------------------
# decks
# --------------------------------------------------------------------------

@post("/session/deck/set-active")
def deck_set_active(req):
    user = _auth(req)
    body = req.json or {}
    deck_id = int(body.get("deck_id") or 0)
    player_id = str(user["player_id"])

    owned = int(user.get("decks_owned") or 0)
    if deck_id < 0 or deck_id >= max(owned, 1):
        raise HttpError(error("DECK_NOT_OWNED", status=400))

    db.run("UPDATE users SET active_deck_id=? WHERE player_id=?", (deck_id, player_id))
    return state.player_details(db.rowdict(
        db.one("SELECT * FROM users WHERE player_id=?", (player_id,))))


@post("/session/deck/edit")
def deck_edit(req):
    user = _auth(req)
    body = req.json or {}
    player_id = str(user["player_id"])
    deck_id = int(body.get("deck_id") or 0)
    morty_ids = [str(x) for x in (body.get("owned_morty_ids") or [])]

    owned = int(user.get("decks_owned") or 0)
    if deck_id < 0 or deck_id >= max(owned, 1):
        raise HttpError(error("DECK_NOT_OWNED", status=400))

    row = db.one("SELECT deck_id FROM decks WHERE player_id=? AND deck_id=?",
                 (player_id, deck_id))
    payload = json.dumps(morty_ids, separators=(",", ":"))
    if row:
        db.run("UPDATE decks SET owned_morty_ids=? WHERE player_id=? AND deck_id=?",
               (payload, player_id, deck_id))
    else:
        db.run("INSERT INTO decks (player_id, deck_id, owned_morty_ids) VALUES (?,?,?)",
               (player_id, deck_id, payload))

    return state.player_details(db.rowdict(
        db.one("SELECT * FROM users WHERE player_id=?", (player_id,))))


@post("/session/decks/slots/buy")
def decks_slots_buy(req):
    user = _auth(req)
    player_id = str(user["player_id"])

    cfg = db.one("SELECT * FROM deck_config WHERE config_id='MP' LIMIT 1")
    cost = int(cfg["cost_additional_slot"]) if cfg else 10
    maximum = int(cfg["max_deck_slots"]) if cfg else 9

    owned = int(user.get("decks_owned") or 0)
    if owned >= maximum:
        raise HttpError(error("MAX_DECK_SLOTS", status=400))
    if int(user.get("coupons") or 0) < cost:
        raise HttpError(error("NOT_ENOUGH_COUPONS", status=400))

    db.run("UPDATE users SET decks_owned=decks_owned+1, coupons=coupons-? "
           "WHERE player_id=?", (cost, player_id))
    db.run("INSERT OR IGNORE INTO decks (player_id, deck_id, owned_morty_ids) "
           "VALUES (?,?,?)", (player_id, owned, "[]"))

    return state.player_details(db.rowdict(
        db.one("SELECT * FROM users WHERE player_id=?", (player_id,))))
