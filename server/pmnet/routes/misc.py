"""Miscellaneous endpoints: healing, shopping, avatars, dimensions, events,
LTE / Moving-Mortys storefronts and inventory actions.

This module is the catch-all for every client call that did not belong in one
of the larger route groups.  Two very different provenances live here and each
handler says which it is:

**Faithful ports** (PHP reference exists in the read-only tree ``/tmp/pmserver``;
JSON key names and semantics were copied from it):

* ``/session/heal``
* ``/session/buy-item``            (PHP is a stub -- see note below)
* ``/session/player-avatar/buy`` / ``set-active`` / ``migrate``
* ``/session/dimensions/query`` / ``dispense-dimension-ftue-rewards``
* ``/session/challenge/list``
* ``/event/check`` / ``/event/brackets`` / ``/event/pendingRewards``
* ``/lte/fyre/events/list`` / ``/moving-mortys/events/list``
* ``/promos/active`` / ``/is-gdpr``
* ``/inappdeletion/get-account-data-storage-time`` /
  ``/inappdeletion/request-account-deletion``
* ``/linkaccount`` / ``/morty/slots/config``

**Reconstructions** (the client calls them but the PHP never implemented them,
or the PHP file is an empty stub).  Shapes below were designed to match the
envelope conventions the PHP *does* use (``{"result": [...]}`` for reward
grants, the full player blob for state mutations, ``{"success": true}`` for
fire-and-forget calls).  Every invented shape is called out in the handler
docstring and the module README at the bottom:

* ``/session/use-item`` / ``/session/use-general-item`` / ``/session/use-shiny-potion``
* ``/session/dispense-item`` / ``/session/recipe/use``
* ``/session/player-flag`` / ``/session/incentive/reject``
* ``/session/owned_morties/learn-attacks`` / ``release-multiple`` /
  ``attempt-release-multiple`` / ``lock`` / ``absorb``
* ``/session/dimensions/buy``
* ``/lte/fyre/battle/summer`` / ``/lte/fyre/purchase/item`` / ``/lte/fyre/purchase/pack``
* ``/moving-mortys/battle`` / ``/moving-mortys/purchase-pass``

Notes that matter for the rest of the server
--------------------------------------------
* ``/morty/slots/config`` is already registered by :mod:`pmnet.routes.session`
  (and ``pmnet/routes/__init__.py`` imports that module first).  Registering it
  again would abort startup with a duplicate-route error, so this module uses a
  *guard* decorator: it registers the PHP-faithful shape only when the path is
  still free.  The existing shape wins in the assembled app.
* ``player_flags`` is a small table created on demand.  It backs
  ``/session/player-flag``, ``/session/incentive/reject`` and the dimension
  FTUE bookkeeping -- state that has no column in the legacy schema.
"""

from __future__ import annotations

import json
import uuid

from .. import db, events, state
from ..http import HttpError, error, get, lookup, post, route

# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

MAX_ITEM_QUANTITY = 10          # gacha/PHP inventory cap per item id
MAX_ATTACKS = 4                 # client will not render a 5th attack

_RAID_EVENT_ID = "RaidBossKillerAsteroid_2025"
_DAILY_END_TIME = "2026-01-28T04:59:59.999Z"

# Reconstructed coin prices (see buy-item).  The PHP buy-item is a hardcoded
# stub that never reads a catalogue, so no authoritative price exists.
DEFAULT_ITEM_PRICE = 100
ITEM_PRICES = {
    "ItemMortyChip": 50,
    "ItemTinCan": 50,
    "ItemBacteriaCell": 50,
    "ItemCircuitBoard": 100,
    "ItemBattery": 100,
    "ItemSerum": 100,
    "ItemParalysisCure": 150,
    "ItemPoisonCure": 150,
    "ItemDarkEnergyBall": 200,
    "ItemCable": 250,
    "ItemGreatSerum": 300,
    "ItemHalzinger": 400,
    "ItemPureSerum": 500,
    "ItemPlutonicRock": 500,
    "ItemMegaSeedAttack": 800,
    "ItemMegaSeedDefence": 800,
    "ItemMegaSeedSpeed": 800,
    "ItemFullRecover": 1000,
    "ItemMrMeeseek": 1000,
    "ItemMegaSeedLevelUp": 1500,
    "ItemShinyPotion": 2000,
}

# Reconstructed item catalogues used by the inventory endpoints.
_HEAL_AMOUNTS = {"ItemSerum": 20, "ItemPureSerum": 20, "ItemGreatSerum": 50}
_FULL_HEAL_ITEMS = {"ItemFullRecover"}
_PP_AMOUNTS = {"ItemBattery": 10}
_SEED_STATS = {
    "ItemMegaSeedAttack": "attack_stat",
    "ItemMegaSeedDefence": "defence_stat",
    "ItemMegaSeedSpeed": "speed_stat",
}

# Reconstructed storefront prices (coupons unless stated otherwise).
DIMENSION_COST = 5
FYRE_ITEM_COST = 5
FYRE_PACK_COST = 20
MOVING_PASS_COST = 10

# Reconstructed recipes keyed by the id the client is expected to send.
RECIPES = {
    "RecipeGreatSerum": {
        "inputs": {"ItemSerum": 2, "ItemPlutonicRock": 1},
        "output": "ItemGreatSerum",
        "output_quantity": 1,
    },
    "RecipeFullRecover": {
        "inputs": {"ItemGreatSerum": 2, "ItemPureSerum": 1},
        "output": "ItemFullRecover",
        "output_quantity": 1,
    },
}


def _auth(req) -> dict:
    """Resolve the player, preferring the JWT/session on the request."""
    if req.player:
        return req.player
    body = req.json if isinstance(req.json, dict) else {}
    sid = body.get("session_id") or req.arg("session_id")
    if sid:
        user = db.rowdict(
            db.one("SELECT * FROM users WHERE session_id=? LIMIT 1", (sid,)))
        if user:
            return user
    raise HttpError(error("NOT_AUTHENTICATED", status=401))


def _body(req) -> dict:
    return req.json if isinstance(req.json, dict) else {}


def _fail(code: str, status: int = 400, **extra):
    raise HttpError(error(code, status=status, **extra))


def _uuid() -> str:
    return str(uuid.uuid4())


def _free(method: str, path: str):
    """Register a route only if nothing else claimed it.

    ``http.route`` raises on duplicates; ``/morty/slots/config`` is owned by
    :mod:`pmnet.routes.session`, so the assembled server must not double-book
    it.  Importing this module on its own still registers the port.
    """
    def deco(fn):
        if lookup(method, path) is None:
            route(method, path)(fn)
        return fn
    return deco


def _fresh(user: dict) -> dict:
    row = db.one("SELECT * FROM users WHERE player_id=?", (str(user["player_id"]),))
    return db.rowdict(row) or user


def _details(user: dict) -> dict:
    """Full player blob, re-read so currency/level changes are reflected."""
    return state.player_details(_fresh(user))


# --------------------------------------------------------------------------
# inventory helpers
# --------------------------------------------------------------------------

def _item_qty(player_id: str, item_id: str) -> int:
    row = db.one(
        "SELECT quantity FROM owned_items WHERE player_id=? AND item_id=? LIMIT 1",
        (player_id, item_id),
    )
    return int(row["quantity"] or 0) if row else 0


def _item_row(player_id: str, item_id: str):
    return db.one(
        "SELECT id, quantity FROM owned_items WHERE player_id=? AND item_id=? LIMIT 1",
        (player_id, item_id),
    )


def _set_item_qty(player_id: str, item_id: str, qty: int) -> None:
    row = _item_row(player_id, item_id)
    if qty <= 0:
        if row:
            db.run("DELETE FROM owned_items WHERE id=?", (row["id"],))
        return
    if row:
        db.run("UPDATE owned_items SET quantity=? WHERE id=?", (qty, row["id"]))
    else:
        db.run(
            "INSERT INTO owned_items (player_id, item_id, quantity) VALUES (?,?,?)",
            (player_id, item_id, qty),
        )


def _grant_item(player_id: str, item_id: str, amount: int,
                cap: int = MAX_ITEM_QUANTITY) -> dict:
    """``owned_items`` upsert with the same per-item cap as gacha/PHP."""
    current = _item_qty(player_id, item_id)
    if current >= cap:
        return {"type": "ITEM", "item_id": item_id, "quantity": current,
                "amount_received": 0, "amount": amount}
    target = min(cap, current + amount)
    _set_item_qty(player_id, item_id, target)
    return {"type": "ITEM", "item_id": item_id, "quantity": target,
            "amount_received": target - current, "amount": amount}


def _consume_item(player_id: str, item_id: str, amount: int = 1) -> int:
    """Remove ``amount`` of an item; returns the remaining quantity."""
    current = _item_qty(player_id, item_id)
    if current < amount:
        _fail("NOT_ENOUGH_ITEMS", status=400, item_id=item_id)
    remaining = current - amount
    _set_item_qty(player_id, item_id, remaining)
    return remaining


def _amount(value, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# owned-morty helpers
# --------------------------------------------------------------------------

def _owned_row(player_id: str, owned_morty_id: str):
    return db.one(
        "SELECT * FROM owned_morties WHERE player_id=? AND owned_morty_id=? LIMIT 1",
        (player_id, owned_morty_id),
    )


def _require_owned(player_id: str, owned_morty_id: str):
    row = _owned_row(player_id, owned_morty_id)
    if not row:
        _fail("MORTY_NOT_OWNED", status=400, owned_morty_id=owned_morty_id)
    return row


def _active_deck_ids(user: dict) -> list[str]:
    player_id = str(user["player_id"])
    deck_id = int(user.get("active_deck_id") or 0)
    deck = db.one(
        "SELECT owned_morty_ids FROM decks WHERE player_id=? AND deck_id=? LIMIT 1",
        (player_id, deck_id),
    )
    return state.decode_ids(deck["owned_morty_ids"]) if deck else []


def _delete_morty(player_id: str, owned_morty_id: str) -> None:
    db.run("DELETE FROM owned_attacks WHERE owned_morty_id=?", (owned_morty_id,))
    db.run("DELETE FROM owned_morties WHERE player_id=? AND owned_morty_id=?",
           (player_id, owned_morty_id))


def _strip_from_decks(player_id: str, owned_morty_ids) -> None:
    """Remove ids from every ``decks.owned_morty_ids`` array the player owns."""
    doomed = {str(x) for x in owned_morty_ids}
    if not doomed:
        return
    for row in db.all_(
            "SELECT deck_id, owned_morty_ids FROM decks WHERE player_id=?",
            (player_id,)):
        current = state.decode_ids(row["owned_morty_ids"])
        kept = [x for x in current if x not in doomed]
        if kept != current:
            db.run(
                "UPDATE decks SET owned_morty_ids=? WHERE player_id=? AND deck_id=?",
                (json.dumps(kept, separators=(",", ":")), player_id,
                 row["deck_id"]),
            )


def _active_owned_id(user: dict) -> str:
    """First active-deck Morty with hp>0, else first owned Morty with hp>0."""
    player_id = str(user["player_id"])
    ids = _active_deck_ids(user)
    if ids:
        holes = ",".join("?" * len(ids))
        hp = {str(r["owned_morty_id"]): int(r["hp"] or 0)
              for r in db.all_(
                  f"SELECT owned_morty_id, hp FROM owned_morties "
                  f"WHERE owned_morty_id IN ({holes})", tuple(ids))}
        for oid in ids:
            if hp.get(oid, 0) > 0:
                return oid
    for morty in state.owned_morties(player_id):
        if morty["hp"] > 0:
            return morty["owned_morty_id"]
    return ""


# --------------------------------------------------------------------------
# player flags (small on-demand table; no legacy column exists)
# --------------------------------------------------------------------------

def _ensure_flags() -> None:
    db.run(
        """CREATE TABLE IF NOT EXISTS player_flags (
               player_id  TEXT NOT NULL,
               flag       TEXT NOT NULL,
               value      TEXT,
               updated_at INTEGER NOT NULL,
               PRIMARY KEY (player_id, flag)
           )"""
    )


def _flag_set(player_id: str, flag: str, value) -> None:
    _ensure_flags()
    text = value if isinstance(value, str) else json.dumps(value)
    db.run(
        """INSERT INTO player_flags (player_id, flag, value, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(player_id, flag)
           DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (player_id, flag, text, db.now()),
    )


def _flag_get(player_id: str, flag: str, default=None):
    _ensure_flags()
    row = db.one(
        "SELECT value FROM player_flags WHERE player_id=? AND flag=? LIMIT 1",
        (player_id, flag),
    )
    if not row or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def _flags_all(player_id: str) -> dict:
    _ensure_flags()
    out: dict = {}
    for row in db.all_(
            "SELECT flag, value FROM player_flags WHERE player_id=? ORDER BY flag",
            (player_id,)):
        try:
            out[str(row["flag"])] = json.loads(row["value"])
        except (TypeError, ValueError):
            out[str(row["flag"])] = row["value"]
    return out


# ==========================================================================
# FAITHFUL PORTS
# ==========================================================================

# --------------------------------------------------------------------------
# /session/heal
# --------------------------------------------------------------------------

def _heal_payload(user: dict, deck_ids: list[str]) -> dict:
    return {
        "player_id": str(user["player_id"]),
        "username": str(user["username"] or ""),
        "player_avatar_id": str(user["player_avatar_id"] or "AvatarRickDefault"),
        "level": int(user["level"] or 1),
        "xp": int(user["xp"] or 0),
        "streak": int(user["streak"] or 0),
        "owned_morties": state.owned_morties(str(user["player_id"]), deck_ids),
        "xp_lower": int(user["xp_lower"] or 0),
        "xp_upper": int(user["xp_upper"] or 0),
        "tags": [],
    }


@post("/session/heal")
def heal(req):
    """Port of ``session/heal/index.php``.

    Restores ``hp = hp_stat`` and ``pp = pp_stat`` for every Morty in the
    active deck and returns the trimmed player blob the PHP returns.  The PHP
    charges nothing and consumes nothing; the only addition here is that, if
    the request names items (``item_id`` or ``item_ids``), one of each that the
    player actually owns is consumed.  This is additive and never blocks the
    heal.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)

    active = int(user.get("active_deck_id") if user.get("active_deck_id")
                 is not None else -1)
    if active < 0:
        _fail("NO_ACTIVE_DECK", status=400)

    deck = db.one(
        "SELECT owned_morty_ids FROM decks WHERE player_id=? AND deck_id=? LIMIT 1",
        (player_id, active),
    )
    deck_ids = state.decode_ids(deck["owned_morty_ids"]) if deck else []
    if not deck_ids:
        _fail("DECK_EMPTY", status=400)

    holes = ",".join("?" * len(deck_ids))
    db.run(
        f"UPDATE owned_morties SET hp=hp_stat "
        f"WHERE player_id=? AND owned_morty_id IN ({holes})",
        (player_id, *deck_ids),
    )
    db.run(
        f"UPDATE owned_attacks SET pp=pp_stat WHERE owned_morty_id IN ({holes})",
        tuple(deck_ids),
    )

    named: list[str] = []
    if body.get("item_id"):
        named.append(str(body["item_id"]))
    if isinstance(body.get("item_ids"), list):
        named.extend(str(i) for i in body["item_ids"] if i)
    for item_id in named:
        if _item_qty(player_id, item_id) > 0:
            _consume_item(player_id, item_id, 1)

    return _heal_payload(_fresh(user), deck_ids)


# --------------------------------------------------------------------------
# /session/buy-item
# --------------------------------------------------------------------------

@post("/session/buy-item")
def buy_item(req):
    """Port of ``session/buy-item/index.php``.

    The PHP handler is a stub -- it never touches the database and echoes a
    hardcoded ``coins``/``coupons`` pair -- but its response *does* tell us the
    currency: purchases are paid with **coins** (the top-level ``coins`` field),
    while gacha pulls use coupons.  We keep the exact key set
    (``coins``/``coupons``/``result``) and implement the deduction the stub
    omitted.  Prices come from :data:`ITEM_PRICES` because the PHP has no
    catalogue.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    item_id = str(body.get("item_id") or "")
    if not item_id:
        _fail("ITEM_MISSING", status=400)

    price = int(ITEM_PRICES.get(item_id, DEFAULT_ITEM_PRICE))
    if int(user.get("coins") or 0) < price:
        _fail("NOT_ENOUGH_COINS", status=400, item_id=item_id, cost=price)

    db.run("UPDATE users SET coins=coins-? WHERE player_id=?", (price, player_id))
    grant = _grant_item(player_id, item_id, 1)
    fresh = _fresh(user)
    return {
        "coins": int(fresh.get("coins") or 0),
        "coupons": int(fresh.get("coupons") or 0),
        "result": {
            "type": "ITEM",
            "item_id": item_id,
            "quantity": grant["quantity"],
            "amount_received": grant["amount_received"],
            "amount": 1,
        },
    }


# --------------------------------------------------------------------------
# /session/player-avatar/*
# --------------------------------------------------------------------------

def _add_avatar(player_id: str, player_avatar_id: str) -> None:
    row = db.one(
        "SELECT id, player_avatar_id FROM owned_avatars WHERE player_id=? LIMIT 1",
        (player_id,),
    )
    if row:
        owned = state.decode_ids(row["player_avatar_id"])
        if player_avatar_id not in owned:
            owned.append(player_avatar_id)
            db.run("UPDATE owned_avatars SET player_avatar_id=? WHERE id=?",
                   (json.dumps(owned, separators=(",", ":")), row["id"]))
    else:
        db.run(
            "INSERT INTO owned_avatars (player_id, player_avatar_id) VALUES (?,?)",
            (player_id, json.dumps([player_avatar_id], separators=(",", ":"))),
        )


@post("/session/player-avatar/buy")
def player_avatar_buy(req):
    """Port of ``session/player-avatar/buy/index.php`` (free unlock + equip).

    ``owned_avatars`` is a single row holding a JSON array, exactly as the PHP
    writes it.  The PHP charges nothing (``coins_deducted`` is hardcoded 0).
    """
    user = _auth(req)
    body = _body(req)
    player_avatar_id = str(body.get("player_avatar_id") or "")
    if not player_avatar_id:
        _fail("MISSING_PARAMETERS", status=400)

    player_id = str(user["player_id"])
    _add_avatar(player_id, player_avatar_id)
    if str(user.get("player_avatar_id") or "") != player_avatar_id:
        db.run("UPDATE users SET player_avatar_id=? WHERE player_id=?",
               (player_avatar_id, player_id))

    return {
        "coins": int(user.get("coins") or 0),
        "coins_deducted": 0,
        "result": {"type": "AVATAR", "player_avatar_id": player_avatar_id},
    }


@post("/session/player-avatar/set-active")
def player_avatar_set_active(req):
    """Port of ``session/player-avatar/set-active/index.php``."""
    user = _auth(req)
    body = _body(req)
    player_avatar_id = str(body.get("player_avatar_id") or "")
    if not player_avatar_id:
        return {"success": False, "error": "Missing parameters"}

    db.run("UPDATE users SET player_avatar_id=? WHERE player_id=?",
           (player_avatar_id, str(user["player_id"])))
    return {"success": True}


@post("/session/player-avatar/migrate")
def player_avatar_migrate(req):
    """Port of ``session/player-avatar/migrate/index.php`` (a no-op).

    The PHP ignores both fields and simply answers ``{"success": true}``; we
    reproduce that behaviour bit for bit.
    """
    _auth(req)
    return {"success": True}


# --------------------------------------------------------------------------
# /session/dimensions/*
# --------------------------------------------------------------------------

@get("/session/dimensions/query")
@post("/session/dimensions/query")
def dimensions_query(req):
    """Port of ``session/dimensions/query/index.php`` (all dimensions owned)."""
    return {
        "dimension_1_owned": True,
        "dimension_2_owned": True,
        "dimension_3_owned": True,
        "dimension_4_owned": True,
        "dimension_5_owned": True,
        "dimension_6_owned": True,
        "dimension_7_owned": True,
    }


@get("/session/dimensions/dispense-dimension-ftue-rewards")
@post("/session/dimensions/dispense-dimension-ftue-rewards")
def dimensions_dispense_ftue(req):
    """Port of ``session/dimensions/dispense-dimension-ftue-rewards``.

    Returns the PHP's exact reward envelope.  Beyond the PHP, the granted
    avatar is recorded and a ``dimension_ftue_rewards_dispensed`` flag is set
    so the companion ``check-...-dispensed`` call (which the PHP never
    implemented) has something to report.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    _add_avatar(player_id, "AvatarFrank")
    _flag_set(player_id, "dimension_ftue_rewards_dispensed", True)
    return {
        "rewards": [{
            "id": "AvatarFrank",
            "amount": 1,
            "type": "AVATAR",
            "player_avatar_id": "AvatarFrank",
        }],
    }


@get("/session/dimensions/check-if-dimension-ftue-rewards-dispensed")
@post("/session/dimensions/check-if-dimension-ftue-rewards-dispensed")
def dimensions_ftue_dispensed(req):
    """Reconstruction: no PHP file exists for this path.

    Returns ``{"dispensed": <bool>}`` backed by the
    ``dimension_ftue_rewards_dispensed`` player flag.
    """
    user = _auth(req)
    dispensed = bool(_flag_get(str(user["player_id"]),
                               "dimension_ftue_rewards_dispensed", False))
    return {"dispensed": dispensed}


# --------------------------------------------------------------------------
# /session/challenge/list
# --------------------------------------------------------------------------

@get("/session/challenge/list")
@post("/session/challenge/list")
def challenge_list(req):
    """Port of ``session/challenge/list/index.php`` (static daily challenges).

    The PHP echoes a fixed array; every key -- including the misspelled
    ``challange_name`` and the ``end_time`` literal -- is preserved.
    """
    return list(_CHALLENGE_LIST)


# --------------------------------------------------------------------------
# /event/*
# --------------------------------------------------------------------------

def _event_payload(event) -> dict:
    return {
        "raid_event_id": str(event["raid_event_id"] or ""),
        "shard_id": str(event["shard_id"] or ""),
        "current_state": str(event["current_state"] or ""),
        "world_id": int(event["world_id"] or 0),
        "spawn_location": str(event["spawn_location"] or ""),
        "boss_id": str(event["boss_id"] or ""),
        "asset_id": str(event["asset_id"] or ""),
        "threat_lvl": int(event["threat_lvl"] or 0),
        "total_damage": str(event["total_damage"] or 0),
        "initial_health": int(event["initial_health"] or 0),
        "max_health_bars": int(event["max_health_bars"] or 0),
        "event_state_next_timestamp": str(event["event_state_next_timestamp"] or ""),
        "has_ran": bool(event["has_ran"]),
        "permit_start": _amount(event["permit_start"], 0),
        "permit_buy_in": _amount(event["permit_buy_in"], 0),
        "ticket_buy_in": _amount(event["ticket_buy_in"], 0),
    }


@post("/event/check")
@get("/event/check")
def event_check(req):
    """Port of ``event/check/index.php`` -- raid-test publisher.

    Validates that the player is in a known room, then broadcasts
    ``shard:raid-boss-state-changed`` into that room's SSE stream.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    room_id = str(user.get("room_id") or "")
    if room_id in ("", "0"):
        _fail("PLAYER_NOT_IN_ROOM", status=409)
    if not db.one("SELECT 1 FROM room_ids WHERE room_id=? LIMIT 1", (room_id,)):
        _fail("ROOM_DOES_NOT_EXIST", status=409, room_id=room_id)

    event = db.one(
        """SELECT * FROM events
            WHERE current_state IN ('build_up','active')
            ORDER BY CASE current_state WHEN 'active' THEN 0
                                        WHEN 'build_up' THEN 1 ELSE 2 END,
                     event_state_next_timestamp ASC
            LIMIT 1"""
    )
    payload = _event_payload(event) if event else {}
    events.publish(room_id, "shard:raid-boss-state-changed", payload)

    db.run("UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE player_id=?",
           (player_id,))
    return {"success": True}


@get("/event/brackets")
@post("/event/brackets")
def event_brackets(req):
    """Port of ``event/brackets/index.php`` (static reward bracket table)."""
    return {"results": [_bracket(row) for row in _BRACKET_ROWS]}


@get("/event/pendingRewards")
@post("/event/pendingRewards")
def event_pending_rewards(req):
    """Port of ``event/pendingRewards/index.php``: always ``NO_REWARDS_PENDING``."""
    raise HttpError(error("NO_REWARDS_PENDING", status=400))


# --------------------------------------------------------------------------
# static / informational endpoints
# --------------------------------------------------------------------------

@get("/lte/fyre/events/list")
@post("/lte/fyre/events/list")
def fyre_events_list(req):
    """Port of ``lte/fyre/events/list/index.php``."""
    return {}


@get("/moving-mortys/events/list")
@post("/moving-mortys/events/list")
def moving_mortys_events_list(req):
    """Port of ``moving-mortys/events/list/index.php``."""
    return {"event_data": [], "next_event_active": "2026-02-02T14:00:00.000Z"}


@get("/promos/active")
@post("/promos/active")
def promos_active(req):
    """Port of ``promos/active/index.php``."""
    return {"SP": {}, "MP": {}}


@get("/is-gdpr")
@post("/is-gdpr")
def is_gdpr(req):
    """Port of ``is-gdpr/index.php``.

    ``GDPR`` must be true.  ``TermsOfServiceUI`` picks between
    ``_acceptGDPRButton`` and ``_acceptCCPAButton`` on this response, and the
    live reference server answers ``{"GDPR": true, "CCPA": false}`` -- matching
    it is what unblocks the terms screen.
    """
    return {"countryCode": "US", "GDPR": True, "CCPA": False}


@get("/inappdeletion/get-account-data-storage-time")
@post("/inappdeletion/get-account-data-storage-time")
def deletion_storage_time(req):
    """Port of ``inappdeletion/get-account-data-storage-time/index.php``."""
    return {"time_anyUnits": 12}


@post("/inappdeletion/request-account-deletion")
@get("/inappdeletion/request-account-deletion")
def request_account_deletion(req):
    """Port of ``inappdeletion/request-account-deletion/index.php``.

    The PHP reads ``uuid`` and echoes an empty body.  We answer with a
    ``{"success": true}`` envelope and record the request as a player flag so
    the account is not silently destroyed on a single unauthenticated POST.
    """
    body = _body(req)
    uuid_value = str(body.get("uuid") or "")
    user = req.player
    if not user:
        sid = body.get("session_id") or req.arg("session_id")
        if sid:
            user = db.rowdict(
                db.one("SELECT * FROM users WHERE session_id=? LIMIT 1", (sid,)))
    if user:
        _flag_set(str(user["player_id"]), "account_deletion_requested",
                  {"uuid": uuid_value, "at": db.iso_now()})
    return {"success": True}


@post("/linkaccount")
@get("/linkaccount")
def linkaccount(req):
    """Port of ``linkaccount/index.php``.

    Stores the client-supplied ``rewards_id`` into ``users.recovery_code_hash``
    exactly as the PHP does (the PHP does not hash it; the field name is
    historical).
    """
    body = _body(req)
    session_id = str(body.get("session_id") or "")
    rewards_id = str(body.get("rewards_id") or "")
    if not session_id or not rewards_id:
        return {"success": False, "error": "MISSING_FIELDS",
                "message": "session_id and recovery_code_hash are required"}

    user = db.rowdict(
        db.one("SELECT * FROM users WHERE session_id=? LIMIT 1", (session_id,)))
    if not user:
        return {"success": False, "error": "SESSION_NOT_FOUND"}
    if user.get("recovery_code_hash"):
        return {"success": True, "message": "Already linked"}

    db.run("UPDATE users SET recovery_code_hash=? WHERE session_id=?",
           (rewards_id, session_id))
    return {"success": True}


_MORTY_SLOTS_CONFIG = {
    "config_id": "MP",
    "starting_morty_slots": 200,
    "max_morty_slots": 1000,
    "increment_slot_count": 50,
    "cost_additional_slot": 5,
}


@_free("GET", "/morty/slots/config")
@_free("POST", "/morty/slots/config")
def morty_slots_config(req):
    """Port of ``morty/slots/config/index.php``.

    Registered only when :mod:`pmnet.routes.session` has not already claimed
    the path (it does in the assembled app, which is why the guard exists).
    """
    return {"config_data": dict(_MORTY_SLOTS_CONFIG)}


# ==========================================================================
# RECONSTRUCTIONS
# ==========================================================================

# --------------------------------------------------------------------------
# item usage
# --------------------------------------------------------------------------

def _apply_item_to_morty(player_id: str, owned_morty_id: str, item_id: str,
                         amount: int = 1) -> bool:
    """Apply an item's effect; returns True if anything changed.

    The client's own ``ItemInfo`` table declares what each item does
    (``effecttype`` + ``effectvalue``), so that is consulted first.  The
    hardcoded tables further down are a fallback for items the table does not
    describe -- they were invented before the real data was available.
    """
    if not owned_morty_id or not _owned_row(player_id, owned_morty_id):
        return False

    try:
        from .. import gamedata
        effect_type, effect_value = gamedata.item_effect(item_id)
    except Exception:  # noqa: BLE001
        effect_type, effect_value = "", 0

    if effect_type == "RestoreHP":
        # An empty value means "restore fully" (ItemPureSerum).
        if effect_value > 0:
            db.run("UPDATE owned_morties SET hp=min(hp_stat, hp+?) "
                   "WHERE player_id=? AND owned_morty_id=?",
                   (effect_value, player_id, owned_morty_id))
        else:
            db.run("UPDATE owned_morties SET hp=hp_stat "
                   "WHERE player_id=? AND owned_morty_id=?",
                   (player_id, owned_morty_id))
        return True

    if effect_type == "RestorePP":
        db.run("UPDATE owned_attacks SET pp=min(pp_stat, pp+?) "
               "WHERE owned_morty_id=?",
               (max(effect_value, 1), owned_morty_id))
        return True

    if effect_type == "Revive":
        # Reviving only means anything for a fainted Morty; the value is how
        # much HP comes back.
        row = _owned_row(player_id, owned_morty_id)
        if int(row.get("hp") or 0) > 0:
            return False
        restored = effect_value if effect_value > 0 else int(row.get("hp_stat") or 1)
        db.run("UPDATE owned_morties SET hp=min(hp_stat, ?) "
               "WHERE player_id=? AND owned_morty_id=?",
               (restored, player_id, owned_morty_id))
        return True

    if effect_type == "Cure":
        # FullRecover also tops up HP and PP; the pure status cures have no
        # effect modelled here because the server does not track conditions.
        if item_id == "ItemFullRecover":
            db.run("UPDATE owned_morties SET hp=hp_stat "
                   "WHERE player_id=? AND owned_morty_id=?",
                   (player_id, owned_morty_id))
            db.run("UPDATE owned_attacks SET pp=pp_stat WHERE owned_morty_id=?",
                   (owned_morty_id,))
        return True

    if effect_type == "StatIncrease" and item_id == "ItemMegaSeedLevelUp":
        db.run("UPDATE owned_morties SET level=level+? "
               "WHERE player_id=? AND owned_morty_id=?",
               (max(1, amount), player_id, owned_morty_id))
        return True

    if item_id in _FULL_HEAL_ITEMS:
        db.run("UPDATE owned_morties SET hp=hp_stat "
               "WHERE player_id=? AND owned_morty_id=?",
               (player_id, owned_morty_id))
        return True
    if item_id in _HEAL_AMOUNTS:
        heal = _HEAL_AMOUNTS[item_id]
        db.run("UPDATE owned_morties SET hp=min(hp_stat, hp+?) "
               "WHERE player_id=? AND owned_morty_id=?",
               (heal, player_id, owned_morty_id))
        return True
    if item_id in _PP_AMOUNTS:
        db.run("UPDATE owned_attacks SET pp=min(pp_stat, pp+?) "
               "WHERE owned_morty_id=?",
               (_PP_AMOUNTS[item_id], owned_morty_id))
        return True
    if item_id in _SEED_STATS:
        column = _SEED_STATS[item_id]
        db.run(f"UPDATE owned_morties SET {column}={column}+? "
               f"WHERE player_id=? AND owned_morty_id=?",
               (max(1, amount), player_id, owned_morty_id))
        return True
    if item_id == "ItemMegaSeedLevelUp":
        db.run("UPDATE owned_morties SET level=level+? "
               "WHERE player_id=? AND owned_morty_id=?",
               (max(1, amount), player_id, owned_morty_id))
        return True
    return False


@post("/session/use-item")
def use_item(req):
    """Reconstruction: consume a targeted item and apply its effect.

    Request: ``{"session_id", "item_id", "owned_morty_id"?, "amount"?}``.
    Response: the full player blob (the same convention
    ``/session/deck/edit`` uses for state mutations).
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    item_id = str(body.get("item_id") or "")
    if not item_id:
        _fail("ITEM_MISSING", status=400)

    amount = _amount(body.get("amount"), 1)
    _consume_item(player_id, item_id, 1)
    _apply_item_to_morty(player_id, str(body.get("owned_morty_id") or ""),
                         item_id, amount)
    return _details(user)


@post("/session/use-general-item")
def use_general_item(req):
    """Reconstruction: consume an untargeted item (stat seeds, level-ups).

    Same request/response shape as ``/session/use-item``.  When
    ``owned_morty_id`` is present the effect is applied to that Morty,
    otherwise the item is simply consumed.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    item_id = str(body.get("item_id") or "")
    if not item_id:
        _fail("ITEM_MISSING", status=400)

    amount = _amount(body.get("amount"), 1)
    _consume_item(player_id, item_id, 1)
    _apply_item_to_morty(player_id, str(body.get("owned_morty_id") or ""),
                         item_id, amount)
    return _details(user)


@post("/session/use-shiny-potion")
def use_shiny_potion(req):
    """Reconstruction: turn a Morty Shiny with a shiny potion.

    Request: ``{"session_id", "owned_morty_id", "item_id"?}`` where ``item_id``
    defaults to ``ItemShinyPotion``.  Response: the full player blob.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    item_id = str(body.get("item_id") or "ItemShinyPotion")
    owned_morty_id = str(body.get("owned_morty_id") or "")

    _consume_item(player_id, item_id, 1)
    if owned_morty_id:
        _require_owned(player_id, owned_morty_id)
        db.run("UPDATE owned_morties SET variant='Shiny' "
               "WHERE player_id=? AND owned_morty_id=?",
               (player_id, owned_morty_id))
    return _details(user)


@post("/session/dispense-item")
def dispense_item(req):
    """Reconstruction: grant items without charging currency.

    Request: ``{"session_id", "item_id", "quantity"?}``.  Response: the full
    player blob.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    item_id = str(body.get("item_id") or "")
    if not item_id:
        _fail("ITEM_MISSING", status=400)
    quantity = max(1, _amount(body.get("quantity") or body.get("amount"), 1))
    _grant_item(player_id, item_id, quantity)
    return _details(user)


@post("/session/recipe/use")
def recipe_use(req):
    """Reconstruction: craft an item from a reconstructed recipe table.

    Request: ``{"session_id", "recipe_id"}`` (``item_id`` is accepted as an
    alias).  Response: the full player blob.  Unknown recipes fail with
    ``RECIPE_UNKNOWN``; missing ingredients fail with ``NOT_ENOUGH_ITEMS``.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    recipe_id = str(body.get("recipe_id") or body.get("item_id") or "")
    recipe = RECIPES.get(recipe_id)
    if not recipe:
        _fail("RECIPE_UNKNOWN", status=400, recipe_id=recipe_id)

    for input_id, needed in recipe["inputs"].items():
        if _item_qty(player_id, input_id) < needed:
            _fail("NOT_ENOUGH_ITEMS", status=400, item_id=input_id)

    for input_id, needed in recipe["inputs"].items():
        _consume_item(player_id, input_id, needed)
    _grant_item(player_id, recipe["output"],
                int(recipe.get("output_quantity") or 1))
    return _details(user)


# --------------------------------------------------------------------------
# player flags / incentives
# --------------------------------------------------------------------------

@post("/session/player-flag")
def player_flag(req):
    """Reconstruction: persist arbitrary per-player flags.

    Request accepts either ``{"flags": {...}}`` (merged) or
    ``{"flag": name, "value": ...}``.  Response:
    ``{"success": true, "flags": {<all flags>}}``.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)

    incoming = body.get("flags")
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            _flag_set(player_id, str(key), value)
    name = body.get("flag") or body.get("name") or body.get("key")
    if name:
        _flag_set(player_id, str(name), body.get("value", True))
    if not incoming and not name:
        _fail("FLAG_MISSING", status=400)

    return {"success": True, "flags": _flags_all(player_id)}


@post("/session/incentive/reject")
def incentive_reject(req):
    """Reconstruction: record that the player declined an incentive.

    Request: ``{"session_id", "incentive_id"?}``.  Response:
    ``{"success": true}``.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    incentive_id = str(body.get("incentive_id") or body.get("id") or "")
    _flag_set(player_id, "incentive_rejected", incentive_id)
    return {"success": True}


# --------------------------------------------------------------------------
# owned_morties actions
# --------------------------------------------------------------------------

def _extract_attack_ids(body: dict) -> list[dict]:
    raw = body.get("attacks")
    if raw is None:
        raw = body.get("owned_attacks")
    if raw is None:
        raw = []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append({"attack_id": entry})
        elif isinstance(entry, dict) and entry.get("attack_id"):
            out.append(entry)
    if not out:
        for key in ("attack_id", "attack_ids"):
            value = body.get(key)
            if isinstance(value, str):
                out.append({"attack_id": value})
            elif isinstance(value, list):
                out.extend({"attack_id": str(v)} for v in value if v)
    return out


@post("/session/owned_morties/learn-attacks")
def learn_attacks(req):
    """Reconstruction: append attacks to a Morty honouring the 4-attack limit.

    Request: ``{"session_id", "owned_morty_id", "attacks": [{"attack_id",
    "pp"?, "pp_stat"?}]}`` (a bare ``attack_id``/``attack_ids`` is accepted).
    New attacks are appended after the highest existing position; once the
    Morty holds four, extra entries are dropped.  Response: full player blob.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    owned_morty_id = str(body.get("owned_morty_id") or "")
    if not owned_morty_id:
        _fail("MORTY_MISSING", status=400)
    _require_owned(player_id, owned_morty_id)

    existing = db.all_(
        "SELECT position FROM owned_attacks WHERE owned_morty_id=? "
        "ORDER BY position ASC",
        (owned_morty_id,),
    )
    next_position = max((int(r["position"] or 0) for r in existing),
                        default=-1) + 1

    for attack in _extract_attack_ids(body):
        if len(existing) >= MAX_ATTACKS:
            break
        attack_id = str(attack.get("attack_id"))
        pp = _amount(attack.get("pp_stat") or attack.get("pp"), 10)
        db.run(
            """INSERT INTO owned_attacks
                   (owned_morty_id, attack_id, position, pp, pp_stat)
               VALUES (?,?,?,?,?)""",
            (owned_morty_id, attack_id, next_position, pp, pp),
        )
        existing.append({"position": next_position})
        next_position += 1

    return _details(user)


def _release_coins(level: int) -> int:
    """Reconstructed release value: 10 coins per level, minimum 10."""
    return max(1, level) * 10


def _release_plan(player_id: str, ids: list[str]) -> tuple[list, list, int]:
    releasable: list[dict] = []
    blocked: list[dict] = []
    total = 0
    for owned_morty_id in ids:
        row = _owned_row(player_id, owned_morty_id)
        if not row:
            blocked.append({"owned_morty_id": owned_morty_id,
                            "reason": "NOT_OWNED"})
            continue
        if state.truthy(row["is_locked"]):
            blocked.append({"owned_morty_id": owned_morty_id,
                            "reason": "LOCKED"})
            continue
        if state.truthy(row["is_trading_locked"]):
            blocked.append({"owned_morty_id": owned_morty_id,
                            "reason": "TRADING_LOCKED"})
            continue
        if state.maybe_null(row["fight_pit_id"]):
            blocked.append({"owned_morty_id": owned_morty_id,
                            "reason": "FIGHT_PIT"})
            continue
        releasable.append({"owned_morty_id": owned_morty_id,
                           "level": int(row["level"] or 1),
                           "coins": _release_coins(int(row["level"] or 1))})
        total += _release_coins(int(row["level"] or 1))
    return releasable, blocked, total


def _release_request_ids(body: dict) -> list[str]:
    raw = body.get("owned_morty_ids")
    if raw is None:
        raw = body.get("owned_morty_id")
    if raw is None:
        raw = body.get("morty_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x) for x in raw if x]


@post("/session/owned_morties/attempt-release-multiple")
def attempt_release_multiple(req):
    """Reconstruction: dry-run of ``release-multiple``.

    Request: ``{"session_id", "owned_morty_ids": [...]}``.  Response:
    ``{"coins", "result": [COIN grant], "released", "blocked"}`` with no
    database mutation.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    releasable, blocked, total = _release_plan(player_id, _release_request_ids(
        _body(req)))
    coins = int(user.get("coins") or 0)
    return {
        "coins": coins,
        "result": [{"type": "COIN", "quantity": coins,
                    "amount_received": total, "amount": total}],
        "released": [r["owned_morty_id"] for r in releasable],
        "blocked": blocked,
    }


@post("/session/owned_morties/release-multiple")
def release_multiple(req):
    """Reconstruction: release Mortys, granting coins.

    Removes each eligible Morty from ``owned_morties`` and ``owned_attacks``,
    strips it from every ``decks.owned_morty_ids`` array and credits
    ``coins = 10 * level``.  Locked / trading-locked / fight-pit Mortys are
    skipped and reported in ``blocked``.  Response mirrors ``/session/buy-item``:
    ``{"coins", "result": [{"type": "COIN", ...}], "released", "blocked"}``.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    releasable, blocked, total = _release_plan(player_id, _release_request_ids(
        _body(req)))

    for entry in releasable:
        _delete_morty(player_id, entry["owned_morty_id"])
    _strip_from_decks(player_id, [r["owned_morty_id"] for r in releasable])
    if total:
        db.run("UPDATE users SET coins=coins+? WHERE player_id=?",
               (total, player_id))

    fresh = _fresh(user)
    coins = int(fresh.get("coins") or 0)
    return {
        "coins": coins,
        "result": [{"type": "COIN", "quantity": coins,
                    "amount_received": total, "amount": total}],
        "released": [r["owned_morty_id"] for r in releasable],
        "blocked": blocked,
    }


@post("/session/owned_morties/lock")
def lock_morty(req):
    """Reconstruction: toggle a Morty's ``is_locked`` flag.

    Request: ``{"session_id", "owned_morty_id", "locked": true}`` (also accepts
    ``is_locked`` / ``lock`` and a list under ``owned_morty_ids``).  Response:
    full player blob.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)

    ids = _release_request_ids(body)
    if not ids:
        _fail("MORTY_MISSING", status=400)

    raw = body.get("locked")
    if raw is None:
        raw = body.get("is_locked")
    if raw is None:
        raw = body.get("lock", True)
    locked = "true" if state.truthy(raw) else "false"

    for owned_morty_id in ids:
        _require_owned(player_id, owned_morty_id)
        db.run("UPDATE owned_morties SET is_locked=? "
               "WHERE player_id=? AND owned_morty_id=?",
               (locked, player_id, owned_morty_id))
    return _details(user)


@post("/session/owned_morties/absorb")
def absorb_morties(req):
    """Reconstruction: the PHP file is an empty stub (``require`` only).

    Request: ``{"session_id", "owned_morty_id" (target),
    "absorb_owned_morty_ids": [...]}``.  Each absorbed Morty is deleted and
    donates ``level * 10`` XP plus one evolution point to the target.  Its ids
    are stripped from every deck.  Response: full player blob.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    target = str(body.get("owned_morty_id") or "")
    if not target:
        _fail("MORTY_MISSING", status=400)
    _require_owned(player_id, target)

    raw = body.get("absorb_owned_morty_ids")
    if raw is None:
        raw = body.get("absorb_owned_morty_id")
    if isinstance(raw, str):
        raw = [raw]
    sources = [str(x) for x in (raw or []) if str(x) and str(x) != target]
    if not sources:
        _fail("NOTHING_TO_ABSORB", status=400)

    gained = 0
    for source in sources:
        row = _require_owned(player_id, source)
        gained += max(1, int(row["level"] or 1)) * 10
        _delete_morty(player_id, source)
    _strip_from_decks(player_id, sources)

    db.run("UPDATE owned_morties SET xp=xp+?, evolution_points=evolution_points+? "
           "WHERE player_id=? AND owned_morty_id=?",
           (gained, len(sources), player_id, target))
    return _details(user)


# --------------------------------------------------------------------------
# /session/dimensions/buy
# --------------------------------------------------------------------------

@post("/session/dimensions/buy")
def dimensions_buy(req):
    """Reconstruction: buy a dimension for coupons.

    Request: ``{"session_id", "dimension_id": <1-7>}``.  Already-owned
    dimensions are free (``/session/dimensions/query`` reports them all as
    owned), so this records the purchase and charges only the first time.
    Response: ``{"success", "dimension_id", "owned", "coins", "coupons"}``.
    """
    user = _auth(req)
    player_id = str(user["player_id"])
    body = _body(req)
    raw = body.get("dimension_id", body.get("dimension_number", 1))
    try:
        dimension_id = int(raw)
    except (TypeError, ValueError):
        _fail("DIMENSION_INVALID", status=400)
    if dimension_id < 1 or dimension_id > 7:
        _fail("DIMENSION_INVALID", status=400, dimension_id=dimension_id)

    flag = f"dimension_{dimension_id}_owned"
    if not _flag_get(player_id, flag, False):
        cost = DIMENSION_COST
        if int(user.get("coupons") or 0) < cost:
            _fail("NOT_ENOUGH_COUPONS", status=400, cost=cost)
        db.run("UPDATE users SET coupons=coupons-? WHERE player_id=?",
               (cost, player_id))
        _flag_set(player_id, flag, True)

    fresh = _fresh(user)
    return {
        "success": True,
        "dimension_id": dimension_id,
        "owned": True,
        "coins": int(fresh.get("coins") or 0),
        "coupons": int(fresh.get("coupons") or 0),
    }


# --------------------------------------------------------------------------
# LTE (Fyre) / Moving-Mortys storefronts and battles
# --------------------------------------------------------------------------

def _spend_coupons(user: dict, cost: int) -> dict:
    player_id = str(user["player_id"])
    if int(user.get("coupons") or 0) < cost:
        _fail("NOT_ENOUGH_COUPONS", status=400, cost=cost)
    db.run("UPDATE users SET coupons=coupons-? WHERE player_id=?",
           (cost, player_id))
    return _fresh(user)


@post("/lte/fyre/purchase/item")
def fyre_purchase_item(req):
    """Reconstruction: buy an LTE item with coupons.

    Request: ``{"session_id", "item_id", "quantity"?}``.  Response mirrors
    gacha: ``{"coupons", "coupons_deducted", "result": [ITEM grant]}``.
    """
    user = _auth(req)
    body = _body(req)
    item_id = str(body.get("item_id") or "")
    if not item_id:
        _fail("ITEM_MISSING", status=400)
    quantity = max(1, _amount(body.get("quantity"), 1))
    cost = FYRE_ITEM_COST * quantity

    fresh = _spend_coupons(user, cost)
    grant = _grant_item(str(user["player_id"]), item_id, quantity)
    return {
        "coupons": int(fresh.get("coupons") or 0),
        "coupons_deducted": cost,
        "result": [grant],
    }


@post("/lte/fyre/purchase/pack")
def fyre_purchase_pack(req):
    """Reconstruction: buy an LTE pack with coupons.

    Request: ``{"session_id", "pack_id"?}``.  Response:
    ``{"coupons", "coupons_deducted", "result": [{type: "PACK", ...}]}``.
    """
    user = _auth(req)
    body = _body(req)
    pack_id = str(body.get("pack_id") or "FyrePack1")
    fresh = _spend_coupons(user, FYRE_PACK_COST)
    return {
        "coupons": int(fresh.get("coupons") or 0),
        "coupons_deducted": FYRE_PACK_COST,
        "result": [{"type": "PACK", "pack_id": pack_id, "quantity": 1,
                    "amount_received": 1, "amount": 1}],
    }


@post("/moving-mortys/purchase-pass")
def moving_mortys_purchase_pass(req):
    """Reconstruction: buy a Moving-Mortys pass with coupons.

    Request: ``{"session_id", "pass_id"?}``.  Response:
    ``{"coupons", "coupons_deducted", "result": [{type: "PASS", ...}]}``.
    """
    user = _auth(req)
    body = _body(req)
    pass_id = str(body.get("pass_id") or "MovingMortysPass")
    fresh = _spend_coupons(user, MOVING_PASS_COST)
    return {
        "coupons": int(fresh.get("coupons") or 0),
        "coupons_deducted": MOVING_PASS_COST,
        "result": [{"type": "PASS", "pass_id": pass_id, "quantity": 1,
                    "amount_received": 1, "amount": 1}],
    }


def _opponent(username: str, avatar: str, morty_id: str, level: int = 5) -> dict:
    owned_morty_id = "00000000-0000-0000-0000-000000000002"
    return {
        "player_id": "317D0000-0000-0000-0000-000000000001",
        "username": username,
        "player_avatar_id": avatar,
        "owned_morties": [{
            "owned_morty_id": owned_morty_id,
            "morty_id": morty_id,
            "level": level,
            "xp": level * level * 5,
            "hp": 100,
            "variant": "Normal",
            "hp_stat": 100,
        }],
        "streak": 0,
        "shiny_if_potion": False,
        "_meta": {
            "isPlayerInDB": False,
            "isControlledByAI": True,
            "isRaidBoss": False,
        },
        "active_owned_morty": owned_morty_id,
    }


def _battle_player(user: dict, session_id: str) -> dict:
    details = state.player_details(user)
    player_id = str(user["player_id"])
    return {
        "player_id": player_id,
        "username": details["username"],
        "player_avatar_id": details["player_avatar_id"],
        "level": details["level"],
        "xp": details["xp"],
        "streak": details["streak"],
        "coins": details["coins"],
        "coupons": details["coupons"],
        "permits": details["permits"],
        "owned_morties": details["owned_morties"],
        "owned_items": details["owned_items"],
        "tags": [],
        "xp_lower": details["xp_lower"],
        "xp_upper": details["xp_upper"],
        "_meta": {
            "session_id": session_id,
            "isPlayerInDB": True,
            "isControlledByAI": False,
            "isRaidBoss": False,
        },
        "active_owned_morty": _active_owned_id(user),
        "move_log": {
            "cooldown": {},
            "count": {},
            "cooldown_next": {"ITEM": 1},
            "last_move_type": {},
        },
    }


def _start_battle(user: dict, session_id: str, battle_type: str,
                  opponent: dict) -> dict:
    """Publish a private ``battle:start`` and acknowledge, like the PHP does."""
    player_id = str(user["player_id"])
    payload = {
        "battle_id": _uuid(),
        "battle_type": battle_type,
        "player": _battle_player(user, session_id),
        "opponent": opponent,
        "meta": {},
    }
    room_id = str(user.get("room_id") or "")
    if room_id:
        events.publish(room_id, "battle:start", payload, player_id)
    return {"success": True}


@post("/lte/fyre/battle/summer")
def fyre_battle_summer(req):
    """Reconstruction: start the Fyre "Summer" battle.

    Mirrors ``session/battle-wild-morty``: a private ``battle:start`` event is
    queued for the player and the HTTP call answers ``{"success": true}``.
    """
    user = _auth(req)
    session_id = str(_body(req).get("session_id") or user.get("session_id") or "")
    return _start_battle(
        user, session_id, "Fyre",
        _opponent("SUMMER", "AvatarSummer", "MortySummer", level=10))


@post("/moving-mortys/battle")
def moving_mortys_battle(req):
    """Reconstruction: start a Moving-Mortys battle (same envelope as Fyre)."""
    user = _auth(req)
    session_id = str(_body(req).get("session_id") or user.get("session_id") or "")
    return _start_battle(
        user, session_id, "MovingMortys",
        _opponent("MOVINGMORTY", "AvatarRickDefault", "MortyDefault"))


# ==========================================================================
# static data (generated verbatim from the PHP literals)
# ==========================================================================

_BRACKET_ROWS = [
    (1, 1, 2, "ITEM", ('ItemPlutonicRock',), 1, None, None),
    (3, 3, 1, "ITEM", ('ItemHalzinger',), 1, None, None),
    (4, 10, 2, "ITEM", ('ItemMortyChip',), 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (201, 300, 100, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (1, 1, 1, "ITEM", ('ItemFullRecover',), 1, None, None),
    (3, 3, 1, "ITEM", ('ItemPlutonicRock',), 1, None, None),
    (4, 10, 1, "ITEM", ('ItemGreatSerum',), 1, None, None),
    (301, 500, 50, "COIN", None, 1, "COUPON", None),
    (2, 2, 4, "ITEM", ('ItemMortyChip',), 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (2, 2, 1, "ITEM", ('ItemFullRecover',), 1, None, None),
    (4, 10, 1, "ITEM", ('ItemHalzinger',), 1, None, None),
    (101, 200, 1, "ITEM", ('ItemMortyChip',), 1, "ITEM", ('ItemMegaSeedAttack',)),
    (301, 500, 50, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (1, 1, 7, "ITEM", ('ItemMortyChip',), 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (3, 3, 1, "ITEM", ('ItemGreatSerum',), 1, None, None),
    (11, 100, 1, "COUPON", None, 1, "COUPON", None),
    (101, 200, 133, "COIN", None, 1, "COUPON", None),
    (301, 500, 50, "COIN", None, 1, "ITEM", ('ItemMegaSeedAttack',)),
    (2, 2, 1, "ITEM", ('ItemGreatSerum',), 1, None, None),
    (3, 3, 1, "MORTY", ('MortyNoDinos',), 1, "ITEM", ('ItemMegaSeedAttack',)),
    (4, 10, 5, "COUPON", None, 1, "COUPON", None),
    (101, 200, 134, "COIN", None, 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (501, 750, 37, "COIN", None, 1, "ITEM", ('ItemMegaSeedAttack',)),
    (1, 1, 2000, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (2, 2, 1450, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (3, 3, 1050, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (11, 100, 1, "MORTY", ('MortyNoDinos',), 1, "ITEM", ('ItemMegaSeedAttack',)),
    (201, 300, 1, "ITEM", ('ItemMortyChip',), 1, "ITEM", ('ItemMegaSeedAttack',)),
    (1, 1, 1, "MORTY", ('MortyNoDinos',), 1, "ITEM", ('ItemMegaSeedAttack',)),
    (1, 1, 2, "ITEM", ('ItemGreatSerum',), 1, None, None),
    (2, 2, 1, "ITEM", ('ItemPlutonicRock',), 1, None, None),
    (4, 10, 1, "ITEM", ('ItemPlutonicRock',), 1, None, None),
    (101, 200, 133, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (301, 500, 50, "COIN", None, 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (2, 2, 1, "MORTY", ('MortyNoDinos',), 1, "ITEM", ('ItemMegaSeedAttack',)),
    (3, 3, 10, "COUPON", None, 1, "COUPON", None),
    (4, 10, 750, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (201, 300, 100, "COIN", None, 1, "COUPON", None),
    (501, 750, 37, "COIN", None, 1, "COUPON", None),
    (1, 1, 2, "ITEM", ('ItemHalzinger',), 1, None, None),
    (2, 2, 1, "ITEM", ('ItemHalzinger',), 1, None, None),
    (4, 10, 1, "MORTY", ('MortyNoDinos',), 1, "ITEM", ('ItemMegaSeedAttack',)),
    (11, 100, 1, "ITEM", ('ItemMortyChip',), 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (501, 750, 39, "COIN", None, 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (1, 1, 35, "COUPON", None, 1, "COUPON", None),
    (2, 2, 20, "COUPON", None, 1, "COUPON", None),
    (3, 3, 3, "ITEM", ('ItemMortyChip',), 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (11, 100, 550, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
    (201, 300, 100, "COIN", None, 1, "ITEM", ('ItemMegaSeedSpeed',)),
    (501, 750, 37, "COIN", None, 1, "ITEM", ('ItemMegaSeedDefence',)),
]

_CHALLENGE_LIST = [
    {'challenge_id': '9e5d1da5-e8d1-46a0-bb66-3ffc6b9c21ee',
     'target': 3,
     'reward_type': 'COIN',
     'reward_amount': 50,
     'reward_id': None,
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'BattleWinPvWM',
     'tag_id': None,
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': '98d200db-fb67-46df-94ea-d73667b52ec0',
     'target': 1,
     'reward_type': 'ITEM',
     'reward_amount': 1,
     'reward_id': 'ItemGreatSerum',
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'PurchaseItemName',
     'tag_id': 'ItemPoisonCure',
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': '7c812c30-29d4-47d1-bd83-31c25ce921ff',
     'target': 1,
     'reward_type': 'COIN',
     'reward_amount': 200,
     'reward_id': None,
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'PurchaseItemName',
     'tag_id': 'ItemGreatSerum',
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': 'a0d87751-c5d3-46a7-b9c6-d12607ec514b',
     'target': 1,
     'reward_type': 'COIN',
     'reward_amount': 500,
     'reward_id': None,
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'UseItemName',
     'tag_id': 'ItemMegaSeedLevelUp',
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': '5433a483-b571-448d-a096-19d097f55447',
     'target': 4,
     'reward_type': 'COIN',
     'reward_amount': 100,
     'reward_id': None,
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'BattleCompleteArena',
     'tag_id': None,
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': '783a59bc-7694-4124-8ca3-baca6f7e9b47',
     'target': 2,
     'reward_type': 'COIN',
     'reward_amount': 200,
     'reward_id': None,
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'EvolveMorty',
     'tag_id': None,
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': 'b2cd638c-ef0c-4c47-bf3f-6b08ef6c07f1',
     'target': 5,
     'reward_type': 'COIN',
     'reward_amount': 100,
     'reward_id': None,
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'WildMorty',
     'tag_id': None,
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': '715473f3-bf86-4310-b5b7-163e324feaa2',
     'target': 4,
     'reward_type': 'COIN',
     'reward_amount': 100,
     'reward_id': None,
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'BattleCompleteArena',
     'tag_id': None,
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': '5c27e488-3a08-43cd-b40e-323168e772eb',
     'target': 5,
     'reward_type': 'ITEM',
     'reward_amount': 1,
     'reward_id': 'ItemCable',
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'BattleCatchPvWMElement',
     'tag_id': 'Rock',
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
    {'challenge_id': '9b99c800-b69f-44e3-b7e1-651644e94b83',
     'target': 1,
     'reward_type': 'ITEM',
     'reward_amount': 1,
     'reward_id': 'ItemParalysisCure',
     'reward_parameters': None,
     'cooldown': 0,
     'is_daily': True,
     'tag': 'UseItemName',
     'tag_id': 'ItemPoisonCure',
     'challange_name': None,
     'progress': 0,
     'cooldown_expires': None,
     'in_cooldown': False,
     'end_time': '2026-01-28T04:59:59.999Z'},
]


def _bracket(row) -> dict:
    (start, end, base_quantity, base_type, base_ids,
     bonus_quantity, bonus_type, bonus_ids) = row
    return {
        "raid_event_id": _RAID_EVENT_ID,
        "reward_bracket_start": start,
        "reward_bracket_end": end,
        "base_quantity": base_quantity,
        "base_type": base_type,
        "base_parameters": {"ids": list(base_ids)} if base_ids else None,
        "bonus_quantity": bonus_quantity,
        "bonus_type": bonus_type,
        "bonus_parameters": {"ids": list(bonus_ids)} if bonus_ids else None,
    }
