"""Room event queue -- the backbone of the SSE stream.

Mirrors the PHP reference exactly, because the client's expectations are
subtle and were clearly hard-won:

* Events are appended to ``event_queue`` with a monotonic id.
* Each player carries a cursor (``users.last_event_id``); the SSE loop polls
  rows with ``id > cursor`` for that player's room.
* ``battle:*`` events are *private* and must only reach the targeted player,
  otherwise every player in the room sees someone else's battle.
* Everything else is broadcast to the room.
* ``room:wild-morty-added`` payloads must carry the full shape (state,
  division, variant, timestamps) or the entity renders but is not clickable.
"""

from __future__ import annotations

import json
import uuid

from . import db

PRIVATE_PREFIX = "battle:"

# Fallback player-id detection for private events whose queue row lacks a target.
_PLAYER_KEYS = (
    "player_id",
    "attacker_player_id",
    "defender_player_id",
    "challenger_player_id",
    "challenged_player_id",
    "owner_player_id",
)


def new_id() -> str:
    return str(uuid.uuid4())


def publish(
    room_id: str,
    event: str,
    payload: dict | list,
    player_id: str | None = None,
) -> int:
    """Append an event and return its queue id."""
    pickup_id = None
    if isinstance(payload, dict):
        candidate = payload.get("pickup_id")
        if isinstance(candidate, str) and len(candidate) == 36:
            pickup_id = candidate

    if event.startswith(PRIVATE_PREFIX):
        # Battle traffic is strictly per-player.
        if not player_id and isinstance(payload, dict):
            player = payload.get("player")
            if isinstance(player, dict):
                pid = player.get("player_id")
                if isinstance(pid, str):
                    player_id = pid
        if not isinstance(player_id, str) or len(player_id) != 36:
            player_id = None
    else:
        player_id = None

    cur = db.run(
        """INSERT INTO event_queue
               (room_id, event_name, payload_json, pickup_id, player_id)
           VALUES (?,?,?,?,?)""",
        (room_id, event, json.dumps(payload, separators=(",", ":"),
                                    ensure_ascii=False), pickup_id, player_id),
    )
    return int(cur.lastrowid or 0)


def payload_involves(payload_json: str, player_id: str) -> bool:
    try:
        data = json.loads(payload_json)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    for key in _PLAYER_KEYS:
        if str(data.get(key, "")) == player_id:
            return True
    turns = data.get("turn_datas")
    if isinstance(turns, list):
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            if str(turn.get("attacker_player_id", "")) == player_id:
                return True
            if str(turn.get("defender_player_id", "")) == player_id:
                return True
    return False


def normalize_wild_morty(payload_json: str) -> str:
    """Guarantee the full entity shape the client needs to make it clickable."""
    try:
        data = json.loads(payload_json)
    except Exception:  # noqa: BLE001
        return payload_json
    if not isinstance(data, dict):
        return payload_json

    wild_id = str(data.get("wild_morty_id") or "")
    morty_id = str(data.get("morty_id") or "")
    placement = data.get("placement")
    if (not wild_id or not morty_id
            or not isinstance(placement, (list, tuple)) or len(placement) != 2):
        return payload_json

    division = data.get("division")
    try:
        division = int(division)
    except (TypeError, ValueError):
        division = 1
    if division <= 0:
        division = 1

    stamp = db.iso_now()
    out = {
        "morty_id": morty_id,
        "placement": [int(placement[0]), int(placement[1])],
        "state": str(data.get("state") or "WORLD"),
        "division": division,
        "variant": str(data.get("variant") or "Normal"),
        "shiny_if_potion": bool(data.get("shiny_if_potion", False)),
        "_created": str(data.get("_created") or stamp),
        "_updated": str(data.get("_updated") or stamp),
        "wild_morty_id": wild_id,
    }
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


def room_max_id(room_id: str) -> int:
    row = db.one("SELECT COALESCE(MAX(id),0) AS m FROM event_queue WHERE room_id=?",
                 (room_id,))
    return int(row["m"]) if row else 0


def cursor_get(player_id: str) -> int:
    row = db.one("SELECT COALESCE(last_event_id,0) AS c FROM users WHERE player_id=?",
                 (player_id,))
    return int(row["c"]) if row else 0


def cursor_set(player_id: str, value: int) -> None:
    db.run("UPDATE users SET last_event_id=?, last_seen=CURRENT_TIMESTAMP "
           "WHERE player_id=?", (value, player_id))


def since(room_id: str, after_id: int, limit: int = 200) -> list[dict]:
    rows = db.all_(
        """SELECT id, event_name, payload_json, player_id AS target_player_id
             FROM event_queue
            WHERE room_id=? AND id>?
            ORDER BY id ASC LIMIT ?""",
        (room_id, after_id, limit),
    )
    return [dict(r) for r in rows]
