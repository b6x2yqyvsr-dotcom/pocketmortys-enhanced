"""Account lifecycle: POST /user/register and POST /user/login.

The original service used a `secret` (a uuid the client stores forever) as the
account credential, then minted a short-lived session that it announced via an
SSE URL.  We keep that exact dance because the client is hard-wired to it.
"""

from __future__ import annotations

import re
import time
import uuid

from .. import cheats, config, db, events, state
from ..http import HttpError, error, json_response, post
from ..jwtutil import signer

# Letters (any script), digits and a few separators.
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff \-.]{1,20}$", re.UNICODE)


def _xp_for(level: int) -> tuple[int, int]:
    """(xp at this level, xp needed for the next), same curve the client uses."""
    try:
        from .. import gamedata
        return gamedata.xp_bounds(level)
    except Exception:  # noqa: BLE001
        return level * level * 28, (level + 1) * (level + 1) * 28


def uuid4() -> str:
    return str(uuid.uuid4())


def _client_error(code: str, status: int = 400):
    return HttpError(error(code, status=status))


# --------------------------------------------------------------------------
# POST /user/register
# --------------------------------------------------------------------------

@post("/user/register")
def register(req):
    body = req.json or {}
    username = (body.get("username") or "").strip()

    if not username:
        raise _client_error("USERNAME_MISSING")
    if not _USERNAME_RE.match(username):
        raise _client_error("USERNAME_INVALID")

    # ---------- recovery-code login path ----------
    # The client re-sends the username; if it matches a stored recovery code
    # hash we hand back the existing secret instead of creating an account.
    recovery = (body.get("recovery_code") or "").strip()
    if recovery:
        owner = _find_by_recovery(recovery)
        if owner:
            return json_response({"secret": owner["secret"]})

    if db.one("SELECT 1 FROM users WHERE username = ? LIMIT 1", (username,)):
        raise _client_error("USERNAME_DUPLICATE")

    avatars = body.get("player_avatar_ids") or []
    if not isinstance(avatars, list) or not avatars:
        avatars = ["AvatarRickDefault"]
    avatars = [str(a) for a in avatars]
    primary_avatar = avatars[0]

    secret = uuid4()
    player_id = uuid4()
    morty_uid = uuid4()
    starter = config.STARTER

    # Player level is normally 1; the private-server option can start everyone
    # at a higher level so the whole map is immediately playable.
    player_level = cheats.start_level()
    xp_lower, xp_upper = _xp_for(player_level)

    db.run(
        """INSERT INTO users
               (secret, player_id, username, player_avatar_id, level, xp, streak,
                active_deck_id, decks_owned, tags, xp_lower, xp_upper,
                coins, coupons, permits, wins, losses, last_event_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (secret, player_id, username, primary_avatar,
         player_level, xp_lower, starter["streak"],
         starter["active_deck_id"], starter["decks_owned"], "[]",
         xp_lower, xp_upper,
         starter["coins"], starter["coupons"], starter["permits"], 0, 0),
    )

    db.run("INSERT INTO decks (player_id, deck_id, owned_morty_ids) VALUES (?,?,?)",
           (player_id, 0, _json([morty_uid])))

    for item_id, qty in (("ItemMortyChip", 1), ("ItemSerum", 1)):
        db.run("INSERT INTO owned_items (player_id, item_id, quantity) VALUES (?,?,?)",
               (player_id, item_id, qty))

    db.run("INSERT INTO owned_avatars (player_id, player_avatar_id) VALUES (?,?)",
           (player_id, _json(avatars)))

    db.run("INSERT INTO mortydex (player_id, morty_id, caught) VALUES (?,?,?)",
           (player_id, "MortyDefault", "true"))

    # Build the starter from the client's own MortyInfo/AttackInfo rather than
    # hardcoded numbers: the reference implementation froze a level-5 Morty at
    # "hp 20 / atk 11 / def 10 / spd 10", which is identical for every Morty
    # and therefore not what the game computes.  Fall back to those values only
    # if the data tables have not been extracted yet.
    level = cheats.STARTER_LEVEL
    try:
        from .. import gamedata
        gamedata.ensure()
        stats = gamedata.basic_stats("MortyDefault", level)
        xp_lower, xp_upper = gamedata.xp_bounds(level)
        moves = gamedata.learnset("MortyDefault", level) or ["AttackOutburst"]
    except Exception:  # noqa: BLE001
        stats = {"hp": 20, "hp_stat": 20, "attack_stat": 11,
                 "defence_stat": 10, "speed_stat": 10}
        xp_lower, xp_upper = 125, 216
        moves = ["AttackOutburst"]

    db.run(
        """INSERT INTO owned_morties
               (player_id, owned_morty_id, morty_id, level, xp, hp, hp_stat,
                attack_stat, defence_stat, variant, speed_stat, is_locked,
                is_trading_locked, fight_pit_id, evolution_points,
                xp_lower, xp_upper)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (player_id, morty_uid, "MortyDefault", level, xp_lower,
         stats["hp"], stats["hp_stat"], stats["attack_stat"],
         stats["defence_stat"], cheats.variant(), stats["speed_stat"],
         "false", "false", "null", 0, xp_lower, xp_upper),
    )

    for position, attack_id in enumerate(moves[:4]):
        try:
            pp = gamedata.attack(attack_id).get("pp") or 12
            pp = 12 if pp < 0 else pp
        except Exception:  # noqa: BLE001
            pp = 12
        db.run(
            """INSERT INTO owned_attacks
                   (owned_morty_id, attack_id, position, pp, pp_stat)
               VALUES (?,?,?,?,?)""",
            (morty_uid, attack_id, position, pp, pp),
        )

    user = db.rowdict(db.one("SELECT * FROM users WHERE player_id = ?", (player_id,)))
    return json_response(state.registration_payload(user, secret))


def _json(value) -> str:
    import json
    return json.dumps(value, separators=(",", ":"))


def _find_by_recovery(code: str) -> dict | None:
    """Recovery codes are compared against `registered_users.recovery_code_hash`
    in the reference.  We store them the same way when one is issued."""
    import hashlib
    digest = hashlib.sha256(code.encode()).hexdigest()
    row = db.one("SELECT * FROM users WHERE recovery_code_hash = ? LIMIT 1", (digest,))
    return db.rowdict(row)


# --------------------------------------------------------------------------
# POST /user/login
# --------------------------------------------------------------------------

@post("/user/login")
def login(req):
    body = req.json or {}
    secret = body.get("secret")

    if not secret:
        raise _client_error("SECRET_MISSING")

    user = db.rowdict(db.one("SELECT * FROM users WHERE secret = ? LIMIT 1", (secret,)))
    if not user:
        raise _client_error("SECRET_INVALID")

    session_id = uuid4()
    now = int(time.time())
    expires = now + config.SESSION_URL_TTL

    db.run("UPDATE users SET session_id = ? WHERE player_id = ?",
           (session_id, user["player_id"]))
    db.run("""INSERT INTO sessions (session_id, player_id, created_at, expires_at)
              VALUES (?,?,?,?)""", (session_id, user["player_id"], now, expires))

    token = signer.encode({
        "player_id": user["player_id"],
        "username": user["username"],
        "level": int(user["level"] or 1),
        "tags": [],
        "session_id": session_id,
        "ping_url": f"{config.public_base()}/session/ping-dynamic",
        "iat": now,
        "exp": expires,
    })

    session_url = f"{config.public_base()}/sse/?token={token}"
    return json_response({
        "session_url": session_url,
        "session_url_ttl": str(config.SESSION_URL_TTL),
    })


# --------------------------------------------------------------------------
# convenience for other modules
# --------------------------------------------------------------------------

@post("/session/ping-dynamic")
def ping_dynamic(req):
    return {"success": True}


def publish_room_state(player_id: str) -> None:
    """Broadcast that a player entered WORLD state (used by join-room)."""
    events.publish(
        db.one("SELECT room_id FROM users WHERE player_id=?", (player_id,))["room_id"],
        "room:user-modified",
        {"player_id": player_id, "state": "WORLD"},
        player_id,
    )
