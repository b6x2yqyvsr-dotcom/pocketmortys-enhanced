"""Player-state serialisation.

``player_details()`` is the single source of truth for "what does this player
look like".  It is used by /user/register, /session/player-details and
/session/state, and its field set was reverse-engineered from the reference
implementation -- the client is picky, and omitting a key tends to show up as a
silent UI failure rather than an error.
"""

from __future__ import annotations

import json

from . import db


def truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def maybe_null(value):
    if value is None:
        return None
    text = str(value).strip()
    return None if text == "" or text.lower() == "null" else text


def decode_ids(raw) -> list[str]:
    """``decks.owned_morty_ids`` has been stored in several shapes over time."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x not in (None, "")]
    text = str(raw).strip()
    if not text:
        return []

    for candidate in (text, text.replace('\\"', '"'), text.strip("\"'")):
        try:
            parsed = json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x not in (None, "")]
        if isinstance(parsed, str) and parsed:
            return [parsed]
    return []


def owned_morties(player_id: str, deck_order: list[str] | None = None) -> list[dict]:
    rows = db.all_(
        """SELECT m.owned_morty_id, m.morty_id, m.level, m.xp, m.hp, m.hp_stat,
                  m.attack_stat, m.defence_stat, m.variant, m.speed_stat,
                  m.is_locked, m.is_trading_locked, m.fight_pit_id,
                  m.evolution_points, m.xp_lower, m.xp_upper,
                  a.attack_id, a.position, a.pp, a.pp_stat
             FROM owned_morties m
             LEFT JOIN owned_attacks a ON m.owned_morty_id = a.owned_morty_id
            WHERE m.player_id = ?
            ORDER BY m.owned_morty_id ASC, a.position ASC""",
        (player_id,),
    )

    by_id: dict[str, dict] = {}
    for row in rows:
        oid = str(row["owned_morty_id"])
        if oid not in by_id:
            by_id[oid] = {
                "owned_morty_id": oid,
                "morty_id": str(row["morty_id"]),
                "level": int(row["level"] or 0),
                "xp": int(row["xp"] or 0),
                "hp": int(row["hp"] or 0),
                "hp_stat": int(row["hp_stat"] or 0),
                "attack_stat": int(row["attack_stat"] or 0),
                "defence_stat": int(row["defence_stat"] or 0),
                "variant": str(row["variant"] or "Normal"),
                "speed_stat": int(row["speed_stat"] or 0),
                "is_locked": truthy(row["is_locked"]),
                "is_trading_locked": truthy(row["is_trading_locked"]),
                "fight_pit_id": maybe_null(row["fight_pit_id"]),
                "evolution_points": int(row["evolution_points"] or 0),
                "owned_attacks": [],
                "xp_lower": int(row["xp_lower"] or 0),
                "xp_upper": int(row["xp_upper"] or 0),
            }
        if row["attack_id"]:
            by_id[oid]["owned_attacks"].append({
                "attack_id": str(row["attack_id"]),
                "position": int(row["position"] or 0),
                "pp": int(row["pp"] or 0),
                "pp_stat": int(row["pp_stat"] or 0),
            })

    if not deck_order:
        return list(by_id.values())

    head = [by_id[oid] for oid in deck_order if oid in by_id]
    head_ids = {m["owned_morty_id"] for m in head}
    return head + [m for m in by_id.values() if m["owned_morty_id"] not in head_ids]


def decks(player_id: str) -> list[dict]:
    rows = db.all_(
        "SELECT deck_id, owned_morty_ids FROM decks WHERE player_id = ?", (player_id,))
    return [
        {
            "deck_id": int(r["deck_id"] or 0),
            "owned_morty_ids": decode_ids(r["owned_morty_ids"]),
        }
        for r in rows
    ]


def owned_items(player_id: str) -> list[dict]:
    rows = db.all_(
        "SELECT item_id, quantity FROM owned_items WHERE player_id = ?", (player_id,))
    return [{"item_id": str(r["item_id"]), "quantity": int(r["quantity"] or 0)}
            for r in rows]


def owned_avatars(player_id: str) -> list[dict]:
    row = db.one(
        "SELECT player_avatar_id FROM owned_avatars WHERE player_id = ? LIMIT 1",
        (player_id,),
    )
    if not row or not row["player_avatar_id"]:
        return []
    return [{"player_avatar_id": a} for a in decode_ids(row["player_avatar_id"])]


def mortydex(player_id: str) -> list[dict]:
    rows = db.all_(
        "SELECT morty_id, caught FROM mortydex WHERE player_id = ?", (player_id,))
    return [{"morty_id": str(r["morty_id"]), "caught": truthy(r["caught"])}
            for r in rows]


def player_details(user: dict) -> dict:
    """Full player blob, as returned by /session/player-details."""
    player_id = str(user["player_id"])
    active_deck_id = int(user.get("active_deck_id") or 0)

    deck_row = db.one(
        "SELECT owned_morty_ids FROM decks WHERE player_id=? AND deck_id=? LIMIT 1",
        (player_id, active_deck_id),
    )
    deck_order = decode_ids(deck_row["owned_morty_ids"]) if deck_row else []

    return {
        "player_id": player_id,
        "username": str(user.get("username") or ""),
        "player_avatar_id": str(user.get("player_avatar_id") or "AvatarRickDefault"),
        "level": int(user.get("level") or 1),
        "xp": int(user.get("xp") or 0),
        "streak": int(user.get("streak") or 0),
        "coins": int(user.get("coins") or 0),
        "coupons": int(user.get("coupons") or 0),
        "permits": int(user.get("permits") or 0),
        "challenge_reward": False,
        "owned_morties": owned_morties(player_id, deck_order),
        "active_deck_id": active_deck_id,
        "decks_owned": int(user.get("decks_owned") or 0),
        "decks": decks(player_id),
        "owned_items": owned_items(player_id),
        "owned_avatars": owned_avatars(player_id),
        "mortydex": mortydex(player_id),
        "tags": [],
        "play_shiny_potion": None,
        "xp_lower": int(user.get("xp_lower") or 0),
        "xp_upper": int(user.get("xp_upper") or 0),
    }


def registration_payload(user: dict, secret: str) -> dict:
    """Response body for POST /user/register (client caches this verbatim)."""
    player_id = str(user["player_id"])
    return {
        "player_id": player_id,
        "username": str(user.get("username") or ""),
        "player_avatar_ids": [a["player_avatar_id"] for a in owned_avatars(player_id)],
        "level": int(user.get("level") or 1),
        "xp": int(user.get("xp") or 0),
        "streak": int(user.get("streak") or 0),
        "owned_morties": owned_morties(player_id),
        "active_deck_id": int(user.get("active_deck_id") or 0),
        "decks_owned": int(user.get("decks_owned") or 0),
        "decks": decks(player_id),
        "owned_items": owned_items(player_id),
        "mortydex": mortydex(player_id),
        "tags": [],
        "xp_lower": int(user.get("xp_lower") or 0),
        "xp_upper": int(user.get("xp_upper") or 0),
        "secret": secret,
    }
