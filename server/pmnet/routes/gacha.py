"""Gacha endpoints.

Ported from the community PHP reference:

* ``session/gacha/index.php``        -> ``POST /session/gacha``
* ``session/gacha-info/index.php``   -> ``POST /session/gacha-info``
* ``sp/gacha-info/index.php``        -> ``POST /sp/gacha-info``
* ``iap/display/list/index.php``     -> ``POST /iap/display/list``

The PHP reference for ``gacha-info`` is a hard-coded blob and the PHP for the
actual pull is annotated "PROMO ONLY": it never consults the division tables.
This port keeps the PHP's wire shapes and pull semantics, but drives everything
from the seeded config tables (``gachas``, ``gacha_contents``,
``gacha_content_items``, ``gacha_promos``, ``gacha_promo_*``,
``gacha_drop_rates``) so the advertised promo/drop rates and the
``division_guarantee`` on each content row are actually honoured:

* a MORTY reward rolls a division weighted by ``gacha_drop_rates.chance``,
  then is floored at the content row's ``division_guarantee``;
* the rolled division is reported back on the result entry (the PHP shape has
  no home for it because the reference never rolled one).

Everything else -- key names, ordering, item cap, coupon deduction, the
``{"error": "..."}`` bodies rather than the ``{"error":{"code":...}}`` envelope
-- matches the PHP byte for byte.
"""

from __future__ import annotations

import json
import random
import uuid

from .. import cheats, db, state
from ..http import HttpError, json_response, post

# From the PHP reference.
MAX_DECK_SIZE = 5
MAX_ITEM_QUANTITY = 10

# Fallback when a promo has no ``gacha_drop_rates`` rows.
DEFAULT_DIVISION = 1

# ``iap/display/list`` is a static catalogue in the reference; there is no IAP
# table in the dump, so it is reproduced verbatim (order matters to the client).
IAP_PRODUCTS = [
    {"iap_id": "IAPMPPicklePack", "featured": True, "onsale": False},
    {"iap_id": "IAPMPCouponPack15", "featured": False, "onsale": False},
    {"iap_id": "IAPMPDimension5Pack", "featured": True, "onsale": False},
    {"iap_id": "IAPMPDimension6Pack", "featured": True, "onsale": False},
    {"iap_id": "IAPMPDimension7Pack", "featured": True, "onsale": False},
    {"iap_id": "IAPMPCouponPack300", "featured": False, "onsale": False},
    {"iap_id": "IAPSPPicklePack", "featured": True, "onsale": False},
    {"iap_id": "IAPSPCouponPack05", "featured": False, "onsale": False},
    {"iap_id": "IAPSPCouponPack15", "featured": False, "onsale": False},
    {"iap_id": "IAPMPCouponPack40", "featured": False, "onsale": False},
    {"iap_id": "IAPSPCouponPack40", "featured": False, "onsale": False},
    {"iap_id": "IAPMPCouponPack05", "featured": False, "onsale": False},
    {"iap_id": "IAPMPCouponPack125", "featured": False, "onsale": False},
    {"iap_id": "IAPSPCouponPack125", "featured": False, "onsale": False},
    {"iap_id": "IAPSPCouponPack300", "featured": False, "onsale": False},
    {"iap_id": "IAPSPJuly4thPack", "featured": True, "onsale": False},
]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _uuid4() -> str:
    return str(uuid.uuid4())


def _fail(status: int, payload: dict) -> HttpError:
    """Build the bare ``{"error": "..."}`` body the gacha PHP emits.

    ``http.error()`` wraps errors as ``{"error": {"code": ...}}``; the gacha
    reference does not, and the client reads the string form here.
    """
    return HttpError(json_response(payload, status=status))


def _auth(req) -> dict:
    """Resolve the player, preferring the credential carried on the request."""
    if req.player:
        return req.player
    body = req.json if isinstance(req.json, dict) else {}
    sid = body.get("session_id") or req.arg("session_id")
    if sid:
        user = db.rowdict(db.one(
            "SELECT * FROM users WHERE session_id = ? LIMIT 1", (sid,)))
        if user:
            return user
    raise _fail(401, {"error": "Invalid session_id"})


def _num_str(value) -> str:
    """The PHP models rates as JSON strings ("0.06"), not numbers."""
    if value is None:
        return "0"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    return "%g" % number


# --------------------------------------------------------------------------
# promo / config lookups
# --------------------------------------------------------------------------

def active_promo() -> dict | None:
    """The promo the gacha currently advertises.

    The PHP requires ``period_start <= now <= period_end`` and otherwise
    returns ``409 NO_ACTIVE_PROMO``.  The shipped seed promo expired before the
    private-server era, which would make the whole gacha permanently dead, so
    when nothing is live we fall back to the most recent promo instead of
    failing.  (Documented deviation -- see the task report.)
    """
    now = db.now()
    row = db.one(
        """SELECT * FROM gacha_promos
            WHERE period_start <= ? AND period_end >= ?
            ORDER BY period_start DESC LIMIT 1""",
        (now, now),
    )
    if row is None:
        row = db.one(
            "SELECT * FROM gacha_promos ORDER BY period_start DESC LIMIT 1")
    return db.rowdict(row)


def _drop_rates(promo_id: str) -> list[tuple[int, int]]:
    rows = db.all_(
        """SELECT division, chance FROM gacha_drop_rates
            WHERE gacha_promo_id = ? ORDER BY division ASC""",
        (promo_id,),
    )
    return [(int(r["division"]), int(r["chance"] or 0)) for r in rows]


def _roll_division(promo_id: str, guarantee) -> int:
    """Weighted division roll, floored by the content row's guarantee."""
    rates = _drop_rates(promo_id)
    if not rates:
        division = DEFAULT_DIVISION
    else:
        weights = [(div, max(0, chance)) for div, chance in rates]
        total = sum(chance for _, chance in weights)
        if total <= 0:
            division = weights[0][0]
        else:
            pick = random.random() * total
            acc = 0.0
            division = weights[-1][0]
            for div, chance in weights:
                acc += chance
                if pick < acc:
                    division = div
                    break

    if guarantee is not None:
        try:
            floor = int(guarantee)
        except (TypeError, ValueError):
            floor = 0
        if floor > division:
            division = floor
    return division


# --------------------------------------------------------------------------
# gacha-info payload (shared by /session/gacha-info and /sp/gacha-info)
# --------------------------------------------------------------------------

def _effect_payload(row) -> dict:
    """One ``gacha_promo_attack_effects`` row in the reference's shape.

    Keys whose column is NULL are omitted, exactly as the PHP blob does.
    """
    effect: dict = {}
    stat = state.maybe_null(row["stat"])
    if stat is not None:
        effect["stat"] = stat
    effect["type"] = str(row["effect_type"] or "")
    if row["power"] is not None:
        effect["power"] = int(row["power"])
    if row["to_self"] is not None:
        effect["to_self"] = bool(int(row["to_self"]))
    if row["accuracy"] is not None:
        effect["accuracy"] = float(row["accuracy"])
    if row["continue_on_miss"] is not None:
        effect["continue_on_miss"] = bool(int(row["continue_on_miss"]))
    return effect


def _promo_content(promo_id: str) -> list[dict]:
    mortys = db.all_(
        """SELECT morty_id, variant, lvl_min, lvl_max, hp_min, hp_max,
                  atk_min, atk_max, def_min, def_max, spd_min, spd_max
             FROM gacha_promo_mortys
            WHERE gacha_promo_id = ? ORDER BY id ASC""",
        (promo_id,),
    )
    attacks = db.all_(
        """SELECT id, morty_id, position, attack_id, pp, pp_stat
             FROM gacha_promo_morty_attacks
            WHERE gacha_promo_id = ? ORDER BY morty_id ASC, position ASC""",
        (promo_id,),
    )
    effects = db.all_(
        """SELECT promo_attack_id, effect_type, stat, power, accuracy,
                  to_self, continue_on_miss
             FROM gacha_promo_attack_effects
            ORDER BY promo_attack_id ASC, id ASC""",
    )

    effects_by_attack: dict[int, list[dict]] = {}
    for row in effects:
        effects_by_attack.setdefault(int(row["promo_attack_id"]), []).append(
            _effect_payload(row))

    attacks_by_morty: dict[str, list[dict]] = {}
    for row in attacks:
        attacks_by_morty.setdefault(str(row["morty_id"]), []).append({
            "attack_id": str(row["attack_id"]),
            "pp_stat": int(row["pp_stat"] or 0),
            "effects": effects_by_attack.get(int(row["id"]), []),
            "position": int(row["position"] or 0),
            "pp": int(row["pp"] or 0),
        })

    out = []
    for row in mortys:
        morty_id = str(row["morty_id"])
        out.append({
            "morty_id": morty_id,
            "speed": [int(row["spd_min"]), int(row["spd_max"])],
            "attack": [int(row["atk_min"]), int(row["atk_max"])],
            "defence": [int(row["def_min"]), int(row["def_max"])],
            "hp": [int(row["hp_min"]), int(row["hp_max"])],
            "level": [int(row["lvl_min"]), int(row["lvl_max"])],
            "owned_attacks": attacks_by_morty.get(morty_id, []),
        })
    return out


def _gacha_content(gacha_id: str) -> list[dict]:
    rows = db.all_(
        """SELECT id, reward, quantity, division_guarantee
             FROM gacha_contents WHERE gacha_id = ? ORDER BY id ASC""",
        (gacha_id,),
    )
    out = []
    for row in rows:
        reward = str(row["reward"] or "")
        guarantee = row["division_guarantee"]
        parameters: dict = {}
        if reward == "ITEM":
            items = db.all_(
                """SELECT item_id FROM gacha_content_items
                    WHERE gacha_content_id = ? ORDER BY id ASC""",
                (int(row["id"]),),
            )
            parameters["ids"] = [str(i["item_id"]) for i in items]
            parameters["division_chances"] = None
        else:
            if guarantee is not None:
                parameters["division_guarantee"] = int(guarantee)
            parameters["division_chances"] = None
        out.append({
            "quantity": int(row["quantity"] or 0),
            "reward": reward,
            "parameters": parameters,
        })
    return out


def _gacha_info_payload() -> dict:
    promo = active_promo()
    gacha_rows = db.all_(
        "SELECT gacha_id, cost, lvl_min, lvl_max FROM gachas ORDER BY id ASC")

    gachas = []
    for row in gacha_rows:
        gacha_id = str(row["gacha_id"])
        gachas.append({
            "gacha_id": gacha_id,
            "cost": cheats.gacha_cost(row["cost"]),
            "gacha_content": _gacha_content(gacha_id),
            "level": [int(row["lvl_min"]), int(row["lvl_max"])],
        })

    if promo:
        promo_id = str(promo["gacha_promo_id"])
        gacha_promo = {
            "gacha_promo_id": promo_id,
            "period": [int(promo["period_start"]), int(promo["period_end"])],
            "image_url": promo["image_url"],
            "drop_chance": _num_str(promo["drop_chance"]),
            "gacha_promo_content": _promo_content(promo_id),
        }
        drop_rates = [chance for _, chance in _drop_rates(promo_id)]
        promo_chance = _num_str(promo["gacha_promo_chance"])
    else:
        gacha_promo = None
        drop_rates = []
        promo_chance = "0"

    return {
        "gacha_promo": gacha_promo,
        "drop_rates": drop_rates,
        "gacha_promo_chance": promo_chance,
        "gacha": gachas,
    }


@post("/session/gacha-info")
def session_gacha_info(req):
    return _gacha_info_payload()


@post("/sp/gacha-info")
def sp_gacha_info(req):
    return _gacha_info_payload()


# --------------------------------------------------------------------------
# inventory / owned-morty grants
# --------------------------------------------------------------------------

def _grant_item(player_id: str, item_id: str, add_qty: int) -> dict:
    """Inventory grant capped at MAX_ITEM_QUANTITY, as the PHP does."""
    row = db.one(
        "SELECT id, quantity FROM owned_items WHERE player_id=? AND item_id=? LIMIT 1",
        (player_id, item_id),
    )
    current = int(row["quantity"] or 0) if row else 0

    if current >= MAX_ITEM_QUANTITY:
        return {"type": "ITEM", "item_id": item_id, "quantity": current,
                "amount_received": 0, "amount": 1}

    target = min(MAX_ITEM_QUANTITY, current + add_qty)
    added = max(0, target - current)

    if row:
        if added > 0:
            db.run("UPDATE owned_items SET quantity=? WHERE id=?",
                   (target, int(row["id"])))
    elif added > 0:
        db.run("INSERT INTO owned_items (player_id, item_id, quantity) VALUES (?,?,?)",
               (player_id, item_id, target))

    return {"type": "ITEM", "item_id": item_id, "quantity": target,
            "amount_received": added, "amount": 1}


def _upsert_mortydex(player_id: str, morty_id: str) -> None:
    row = db.one(
        "SELECT id FROM mortydex WHERE player_id=? AND morty_id=? LIMIT 1",
        (player_id, morty_id),
    )
    if row:
        db.run("UPDATE mortydex SET caught='true' WHERE id=?", (int(row["id"]),))
    else:
        db.run("INSERT INTO mortydex (player_id, morty_id, caught) VALUES (?,?, 'true')",
               (player_id, morty_id))


def _insert_owned_morty(player_id: str, morty_id: str, level: int, variant: str,
                        hp: int, atk: int, defence: int, speed: int,
                        attacks: list[dict]) -> dict:
    """Fresh uuid4 morty + its promo attacks + a mortydex entry."""
    owned_morty_id = _uuid4()
    xp = int(round((level * level) * 28.0))

    db.run(
        """INSERT INTO owned_morties
               (player_id, owned_morty_id, morty_id, level, xp, hp, hp_stat,
                attack_stat, defence_stat, variant, speed_stat, is_locked,
                is_trading_locked, fight_pit_id, evolution_points,
                xp_lower, xp_upper)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'false', 'false', 'null', 0, ?, ?)""",
        (player_id, owned_morty_id, morty_id, level, xp, hp, hp,
         atk, defence, variant, speed, xp, xp),
    )

    owned_attacks = []
    for attack in attacks:
        db.run(
            """INSERT INTO owned_attacks
                   (owned_morty_id, position, attack_id, pp, pp_stat)
               VALUES (?,?,?,?,?)""",
            (owned_morty_id, int(attack["position"]), str(attack["attack_id"]),
             int(attack["pp"]), int(attack["pp_stat"])),
        )
        owned_attacks.append({
            "attack_id": str(attack["attack_id"]),
            "pp": int(attack["pp"]),
            "position": int(attack["position"]),
        })

    _upsert_mortydex(player_id, morty_id)

    return {
        "owned_morty_id": owned_morty_id,
        "player_id": player_id,
        "morty_id": morty_id,
        "level": level,
        "xp": xp,
        "hp": hp,
        "hp_stat": hp,
        "attack_stat": atk,
        "defence_stat": defence,
        "speed_stat": speed,
        "variant": variant,
        "owned_attacks": owned_attacks,
    }


def _perk_active_deck(player_id: str, owned_morty_id: str) -> bool:
    """Port of maybeAddToActiveDeck(): first 5 pulls auto-fill the deck."""
    row = db.one("SELECT active_deck_id FROM users WHERE player_id=? LIMIT 1",
                 (player_id,))
    deck_id = int(row["active_deck_id"] or 0) if row else 0

    deck = db.one(
        "SELECT owned_morty_ids FROM decks WHERE player_id=? AND deck_id=? LIMIT 1",
        (player_id, deck_id),
    )
    ids = state.decode_ids(deck["owned_morty_ids"]) if deck else []

    if owned_morty_id in ids:
        return True
    if len(ids) >= MAX_DECK_SIZE:
        return False

    ids.append(owned_morty_id)
    payload = json.dumps(ids, separators=(",", ":"))
    cur = db.run(
        "UPDATE decks SET owned_morty_ids=? WHERE player_id=? AND deck_id=?",
        (payload, player_id, deck_id),
    )
    if cur.rowcount == 0:
        db.run("INSERT INTO decks (player_id, deck_id, owned_morty_ids) VALUES (?,?,?)",
               (player_id, deck_id, payload))
    return True


# --------------------------------------------------------------------------
# the pull
# --------------------------------------------------------------------------

@post("/session/gacha")
def session_gacha(req):
    body = req.json if isinstance(req.json, dict) else {}
    session_id = str(body.get("session_id") or req.arg("session_id") or "")
    gacha_id = str(body.get("gacha_id") or req.arg("gacha_id") or "")

    if not session_id or not gacha_id:
        raise _fail(400, {"error": "Missing session_id or gacha_id"})

    player = db.rowdict(db.one(
        "SELECT * FROM users WHERE session_id = ? LIMIT 1", (session_id,)))
    if not player and req.player:
        player = req.player
    if not player:
        raise _fail(401, {"error": "Invalid session_id"})
    player_id = str(player["player_id"])

    gacha = db.one(
        "SELECT gacha_id, cost, lvl_min, lvl_max FROM gachas WHERE gacha_id=? LIMIT 1",
        (gacha_id,),
    )
    if not gacha:
        raise _fail(404, {"error": "Unknown gacha_id", "gacha_id": gacha_id})

    contents = db.all_(
        """SELECT id, reward, quantity, division_guarantee
             FROM gacha_contents WHERE gacha_id=? ORDER BY id ASC""",
        (gacha_id,),
    )
    if not contents:
        raise _fail(500, {"error": "Gacha has no content", "gacha_id": gacha_id})

    promo = active_promo()
    if not promo:
        raise _fail(409, {"error": "NO_ACTIVE_PROMO"})
    promo_id = str(promo["gacha_promo_id"])

    cost = cheats.gacha_cost(gacha["cost"])
    item_results: list[dict] = []
    morty_results: list[dict] = []

    db.run("BEGIN")
    try:
        # 0) coupons first, atomically
        if cost <= 0:
            new_coupons = int(player["coupons"] or 0)
        else:
            cur = db.run(
                "UPDATE users SET coupons = coupons - ? WHERE player_id=? AND coupons >= ?",
                (cost, player_id, cost),
            )
            if cur.rowcount != 1:
                raise HttpError(json_response(
                    {"error": "NOT_ENOUGH_COUPONS"}, status=403))
            row = db.one("SELECT coupons FROM users WHERE player_id=? LIMIT 1",
                         (player_id,))
            new_coupons = int(row["coupons"] or 0) if row else 0

        for content in contents:
            reward = str(content["reward"] or "")
            quantity = int(content["quantity"] or 0)

            if reward == "ITEM":
                for _ in range(quantity):
                    items = db.all_(
                        """SELECT item_id FROM gacha_content_items
                            WHERE gacha_content_id = ? ORDER BY id ASC""",
                        (int(content["id"]),),
                    )
                    if not items:
                        raise RuntimeError("GACHA_CONTENT_HAS_NO_ITEMS")
                    item_id = str(random.choice(items)["item_id"])
                    item_results.append(_grant_item(player_id, item_id, 1))
                continue

            if reward == "MORTY":
                promo_mortys = db.all_(
                    """SELECT morty_id, variant, lvl_min, lvl_max, hp_min, hp_max,
                              atk_min, atk_max, def_min, def_max, spd_min, spd_max
                         FROM gacha_promo_mortys WHERE gacha_promo_id=?""",
                    (promo_id,),
                )
                if not promo_mortys:
                    raise RuntimeError("PROMO_HAS_NO_MORTYS")

                for _ in range(quantity):
                    pm = random.choice(promo_mortys)
                    morty_id = str(pm["morty_id"])
                    variant = cheats.variant()
                    if variant == "Normal":
                        variant = str(pm["variant"] or "Normal")

                    level = random.randint(int(pm["lvl_min"]), int(pm["lvl_max"]))
                    hp = random.randint(int(pm["hp_min"]), int(pm["hp_max"]))
                    atk = random.randint(int(pm["atk_min"]), int(pm["atk_max"]))
                    defence = random.randint(int(pm["def_min"]), int(pm["def_max"]))
                    speed = random.randint(int(pm["spd_min"]), int(pm["spd_max"]))

                    attack_rows = db.all_(
                        """SELECT position, attack_id, pp, pp_stat
                             FROM gacha_promo_morty_attacks
                            WHERE gacha_promo_id=? AND morty_id=?
                            ORDER BY position ASC""",
                        (promo_id, morty_id),
                    )
                    if not attack_rows:
                        raise RuntimeError("PROMO_MORTY_HAS_NO_ATTACKS")
                    attacks = [{
                        "position": int(a["position"]),
                        "attack_id": str(a["attack_id"]),
                        "pp": int(a["pp"] or 0),
                        "pp_stat": int(a["pp_stat"] or 0),
                    } for a in attack_rows]

                    division = _roll_division(promo_id, content["division_guarantee"])

                    owned = _insert_owned_morty(
                        player_id, morty_id, level, variant,
                        hp, atk, defence, speed, attacks)
                    added = _perk_active_deck(player_id, owned["owned_morty_id"])

                    morty_results.append({
                        "type": "MORTY",
                        "morty_id": morty_id,
                        "level": level,
                        "division": division,
                        "added_to_active_deck": added,
                        "owned_morty_limit_reached": False,
                        "variant": variant,
                        "owned_morty": owned,
                    })
                continue

        db.run("COMMIT")
    except HttpError:
        db.run("ROLLBACK")
        raise
    except Exception as exc:  # noqa: BLE001
        db.run("ROLLBACK")
        raise _fail(500, {"error": "pull_failed", "detail": str(exc)}) from exc

    return {
        "coupons": new_coupons,
        "coupons_deducted": cost,
        "result": item_results + morty_results,
    }


# --------------------------------------------------------------------------
# IAP catalogue
# --------------------------------------------------------------------------

@post("/iap/display/list")
def iap_display_list(req):
    return {"products": [dict(p) for p in IAP_PRODUCTS]}
