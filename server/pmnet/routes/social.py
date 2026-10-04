"""Social, arena, shard-score and trading endpoints.

This module owns four endpoint families that the PHP reference groups loosely:

* friends  -- ``/session/friend/*``
* emotes   -- ``/session/emote/room``
* arena    -- ``/arena/*`` (bot battles, energy, events, rewards, scores)
* shard    -- ``/shard/*``
* trading  -- ``/trading/*``

Provenance
----------
**Faithful ports** (the PHP reference exists in the read-only tree
``/tmp/pmserver``; JSON key names, error envelopes and semantics were copied
from it).  Where the PHP file only echoed a hard-coded blob the *shape* is
preserved verbatim and the switch to live data is called out in the handler:

* ``/session/friend/list``
* ``/session/friend/search``
* ``/session/friend/request``
* ``/session/friend/response``
* ``/session/friend/remove``
* ``/session/emote/room``
* ``/arena/energy/fetch``
* ``/arena/events/list``           (PHP echoes one hard-coded event)
* ``/arena/rewards/list``          (PHP echoes 56 hard-coded brackets)
* ``/arena/rewards/pending/list``
* ``/arena/scores/list``           (PHP echoes a hard-coded league table --
                                   the key set is preserved, the rows are live)
* ``/shard/scores``                (same: PHP echoes a hard-coded leaderboard)
* ``/trading/config``

**RECONSTRUCTIONS** -- the client calls these but no PHP file exists (or the
PHP is an input-ignoring stub with no state behind it).  The shapes below were
designed to match the envelope conventions the PHP *does* use
(``{"error": {"code": ...}}`` with an HTTP status for failures,
``{"success": true, ...}`` for fire-and-forget calls, ``snake_case`` keys,
``_created``/``_modified`` ISO-8601 UTC timestamps with millisecond precision).
Every invented key is listed here:

* ``/session/friend/donate-morty``
* ``/session/friend/request-morty``
* ``/arena/battle/bot``            (PHP stub -- same keys, live-ish values)
* ``/shard/health``
* ``/trading/get-trades``
* ``/trading/offer-morty``
* ``/trading/offer-response``
* ``/trading/request-morty``
* ``/trading/cancel-trade``
* ``/trading/cancel-trade-offer``

Notes that matter for the rest of the server
--------------------------------------------
* ``/trading/config`` is *also* registered by :mod:`pmnet.routes.session` as an
  unfaithful placeholder (``max_trades`` / ``trade_level_required`` /
  ``trade_cooldown_seconds``).  The PHP answers ``config_id``,
  ``premium_trade_cost``, ``max_trades``, ``trade_cooldown_minutes``.  Because
  this module must present the PHP shape, ``_claim`` replaces that one
  ``session``-owned route at import time and leaves every other module's
  routes alone.  If ``session.trading_config`` is ever deleted this keeps
  working unchanged.
* ``/session/battle-invite/*`` is deliberately **not** implemented here; the
  battle module owns that family.
* Trading state lives in a ``trades`` table created on first use (the legacy
  schema has no equivalent).  Donation state reuses the existing
  ``users.donation_request`` text column, which is now stored as JSON.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from .. import db, events, http, state
from ..http import HttpError, Response, error, lookup, post, route

# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

# The arena event id the PHP hard-codes in arena/events/list and
# arena/scores/list; the raid event id matches pmnet.routes.misc.
ARENA_EVENT_ID = "dace4578-cdca-11ef-a9a1-3f079e2d6aa5"
ARENA_SCORES_ID = "dace41f4-cdca-11ef-acec-8f82efc7517f"
RAID_EVENT_ID = "RaidBossKillerAsteroid_2025"
ARENA_SHARD_ID = 3
ARENA_PROMOTION_TROPHIES = 161
ARENA_RELEGATION_TROPHIES = 128
FRIEND_LIMIT = 30
FRIEND_POLL_PERIOD = 5
ONLINE_WINDOW_SECONDS = 300
TRADE_PREMIUM_COST = 3
TRADE_MAX = 3
TRADE_COOLDOWN_MINUTES = 1440
ENERGY_AMOUNT = 100

# ``friend_list.pending`` has been written as both TEXT ('true'/'false', the
# PHP) and INTEGER (1/0) by different tools, so accept either.
_PENDING_SQL = "LOWER(COALESCE(r.pending,'')) IN ('true','1')"
_NOT_PENDING_SQL = "LOWER(COALESCE(r.pending,'')) NOT IN ('true','1')"


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


def _maybe_auth(req) -> dict | None:
    try:
        return _auth(req)
    except HttpError:
        return None


def _body(req) -> dict:
    return req.json if isinstance(req.json, dict) else {}


def _fail(code: str, status: int = 400, **extra):
    raise HttpError(error(code, status=status, **extra))


def _uuid() -> str:
    return str(uuid.uuid4())


def _claim(method: str, path: str):
    """Register a route, replacing a known placeholder owner.

    ``http.route`` raises on duplicates.  ``/trading/config`` is currently
    registered by :mod:`pmnet.routes.session` with a non-PHP shape; the PHP
    shape is authoritative, so take the path over -- but only from that
    module, so a future owner is never silently overwritten.
    """
    def deco(fn):
        existing = lookup(method, path)
        if existing is not None:
            if getattr(existing, "__module__", "") != "pmnet.routes.session":
                return fn
            http._ROUTES.pop((method.upper(), path.rstrip("/") or "/"), None)
        route(method, path)(fn)
        return fn
    return deco


def _raw(body: bytes, status: int = 200, content_type: str = "application/json; charset=utf-8") -> Response:
    return Response(status=status, body=body, content_type=content_type)


def _etag(response: Response) -> Response:
    response.headers["ETag"] = 'W/"' + hashlib.md5(response.body).hexdigest() + '"'
    return response


def _user(player_id: str):
    return db.one("SELECT * FROM users WHERE player_id=? LIMIT 1", (player_id,))


def _user_json(row) -> dict:
    """The compact player card every friend-facing endpoint returns."""
    return {
        "username": str(row["username"] or ""),
        "player_avatar_id": str(row["player_avatar_id"] or "AvatarRickDefault"),
        "level": int(row["level"] or 1),
        "player_id": str(row["player_id"]),
    }


def _cutoff(seconds: int = ONLINE_WINDOW_SECONDS) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(db.now() - seconds))


def _iso(value) -> str:
    """Normalise a stored timestamp to the PHP's ISO-8601 UTC shape.

    ``friend_list.created`` is written as ``iso_now()`` but ``users.last_seen``
    is written by SQLite's ``CURRENT_TIMESTAMP`` (``YYYY-MM-DD HH:MM:SS``), and
    the client parses both as the same field.
    """
    if not value:
        return db.iso_now()
    text = str(value)
    if "T" in text:
        return text
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.strptime(text[:26], fmt)) + ".000Z"
        except ValueError:
            continue
    return text


# --------------------------------------------------------------------------
# /session/friend/*
# --------------------------------------------------------------------------

@post("/session/friend/list")
def friend_list(req):
    """Port of ``session/friend/list/index.php``.

    Three queries join ``users`` onto ``friend_list``: requests I sent, requests
    I received (both ``pending``), and accepted friends.  ``online`` is a
    five-minute ``last_seen`` window and is emitted as the *strings*
    ``"true"``/``"false"`` -- the PHP's ``CASE`` produced those exact strings
    and the client parses them.
    """
    me = _auth(req)
    player_id = str(me["player_id"])
    cutoff = _cutoff()

    sent = db.all_(
        f"""
        SELECT p.username, p.player_avatar_id, p.level, p.player_id,
               r.created AS _created
          FROM friend_list r
          JOIN users p ON p.player_id = r.player_id_b
         WHERE r.player_id_a = ?
           AND {_PENDING_SQL}
         ORDER BY r.created DESC
        """,
        (player_id,),
    )
    received = db.all_(
        f"""
        SELECT p.username, p.player_avatar_id, p.level, p.player_id,
               r.created AS _created
          FROM friend_list r
          JOIN users p ON p.player_id = r.player_id_a
         WHERE r.player_id_b = ?
           AND {_PENDING_SQL}
         ORDER BY r.created DESC
        """,
        (player_id,),
    )
    friends = db.all_(
        f"""
        SELECT p.username, p.player_avatar_id, p.level, p.player_id,
               COALESCE(p.wins, 0) AS wins,
               COALESCE(p.losses, 0) AS losses,
               CASE WHEN COALESCE(p.last_seen,'') >= ? THEN 'true'
                    ELSE 'false' END AS online,
               p.donation_request
          FROM friend_list r
          JOIN users p ON p.player_id =
                CASE WHEN r.player_id_a = ? THEN r.player_id_b
                     ELSE r.player_id_a END
         WHERE ? IN (r.player_id_a, r.player_id_b)
           AND {_NOT_PENDING_SQL}
         ORDER BY p.username ASC
        """,
        (cutoff, player_id, player_id),
    )

    return {
        "requests_sent": [
            {"username": str(r["username"] or ""),
             "player_avatar_id": str(r["player_avatar_id"] or "AvatarRickDefault"),
             "level": int(r["level"] or 1),
             "player_id": str(r["player_id"]),
             "_created": _iso(r["_created"])}
            for r in sent
        ],
        "requests_received": [
            {"username": str(r["username"] or ""),
             "player_avatar_id": str(r["player_avatar_id"] or "AvatarRickDefault"),
             "level": int(r["level"] or 1),
             "player_id": str(r["player_id"]),
             "_created": _iso(r["_created"])}
            for r in received
        ],
        "friends": [
            {"username": str(r["username"] or ""),
             "player_avatar_id": str(r["player_avatar_id"] or "AvatarRickDefault"),
             "level": int(r["level"] or 1),
             "player_id": str(r["player_id"]),
             "wins": int(r["wins"] or 0),
             "losses": int(r["losses"] or 0),
             "donation_request": _decode_donation(r["donation_request"]),
             "online": str(r["online"])}
            for r in friends
        ],
        "poll_period": FRIEND_POLL_PERIOD,
        "donation_request": _my_donation_request(me),
        "limit_friends": FRIEND_LIMIT,
    }


@post("/session/friend/search")
def friend_search(req):
    """Port of ``session/friend/search/index.php``.

    Exact-username lookup.  When the name is unknown the PHP echoes *nothing*
    (an empty 200 body) rather than ``{}`` -- kept verbatim so the client's
    "no result" path is exercised.
    """
    _auth(req)  # the PHP authenticates here but never echoes the identity
    body = _body(req)
    query = body.get("query")
    if query is None:
        query = req.arg("query")
    if not query:
        _fail("MISSING_QUERY", status=400)

    row = db.one("SELECT * FROM users WHERE username=? LIMIT 1", (str(query),))
    if not row:
        return _raw(b"")
    return _etag(http.json_response(_user_json(row)))


@post("/session/friend/request")
def friend_request(req):
    """Port of ``session/friend/request/index.php``.

    Inserts ``player_id_a`` = sender, ``player_id_b`` = target with
    ``pending='true'`` and ``direction='false'`` (the PHP stores those as text
    but answers with real booleans).  A row in *either* orientation is treated
    as a duplicate.
    """
    body = _body(req)
    me = _auth(req)
    sender_id = str(me["player_id"])
    target_id = body.get("player_id") or req.arg("player_id")
    if not target_id:
        _fail("MISSING_PLAYER_ID", status=400)
    target_id = str(target_id)

    if sender_id == target_id:
        _fail("FRIEND_SELF", status=400)

    target = _user(target_id)
    if not target:
        _fail("PLAYER_NOT_FOUND", status=404)

    exists = db.one(
        """SELECT 1 FROM friend_list
            WHERE (player_id_a=? AND player_id_b=?)
               OR (player_id_a=? AND player_id_b=?)
            LIMIT 1""",
        (sender_id, target_id, target_id, sender_id),
    )
    if exists:
        _fail("FRIEND_DUPLICATE", status=400)

    date = db.iso_now()
    db.run(
        """INSERT INTO friend_list
               (player_id_a, player_id_b, pending, direction, created, modified)
           VALUES (?,?,?,?,?,?)""",
        (sender_id, target_id, "true", "false", date, date),
    )
    return {
        "player_id_a": sender_id,
        "player_id_b": target_id,
        "wins_a": int(me.get("wins") or 0),
        "wins_b": int(target["wins"] or 0),
        "pending": True,
        "direction": False,
        "_created": date,
        "_modified": date,
    }


@post("/session/friend/response")
def friend_response(req):
    """Port of ``session/friend/response/index.php``.

    ``approve`` must be a real boolean.  Approving flips ``pending`` to
    ``'false'``; rejecting deletes the row.  Either way the PHP echoes the
    literal ``1`` (not JSON), so we do too.
    """
    body = _body(req)
    me = _auth(req)
    me_id = str(me["player_id"])

    sender_id = body.get("player_id") or req.arg("player_id")
    approve = body.get("approve")
    if not sender_id or not isinstance(approve, bool):
        return _raw(json.dumps({
            "error": "Missing/invalid fields",
            "required": {"session_id": "string", "player_id": "string",
                         "approve": "boolean"},
        }, separators=(",", ":")).encode(), status=400)

    sender_id = str(sender_id)
    if me_id == sender_id:
        return _raw(json.dumps(
            {"error": "player_id cannot be the same as the authenticated user"},
            separators=(",", ":")).encode(), status=400)

    found = db.one(
        f"""SELECT player_id_a, player_id_b FROM friend_list r
             WHERE {_PENDING_SQL}
               AND ((player_id_a=? AND player_id_b=?)
                 OR (player_id_a=? AND player_id_b=?))
             ORDER BY r.created DESC LIMIT 1""",
        (sender_id, me_id, me_id, sender_id),
    )
    if not found:
        return _raw(json.dumps({"error": "No pending friend request found"},
                               separators=(",", ":")).encode(), status=400)

    if approve:
        db.run(
            """UPDATE friend_list
                  SET pending='false', modified=?
                WHERE LOWER(COALESCE(pending,'')) IN ('true','1')
                  AND ((player_id_a=? AND player_id_b=?)
                    OR (player_id_a=? AND player_id_b=?))""",
            (db.iso_now(), sender_id, me_id, me_id, sender_id),
        )
    else:
        db.run(
            """DELETE FROM friend_list
                WHERE LOWER(COALESCE(pending,'')) IN ('true','1')
                  AND ((player_id_a=? AND player_id_b=?)
                    OR (player_id_a=? AND player_id_b=?))""",
            (sender_id, me_id, me_id, sender_id),
        )
    return _raw(b"1")


@post("/session/friend/remove")
def friend_remove(req):
    """Port of ``session/friend/remove/index.php``.

    Deletes the single friendship row in either orientation.  Not-found is a
    404 carrying ``{"success": false, ...}``; success carries the removed
    player id.
    """
    body = _body(req)
    me = _auth(req)
    me_id = str(me["player_id"])

    friend_id = body.get("player_id") or req.arg("player_id")
    if not friend_id:
        return _raw(json.dumps({"error": "Missing session_id or friend_id"},
                               separators=(",", ":")).encode(), status=400)
    friend_id = str(friend_id)
    if me_id == friend_id:
        return _raw(json.dumps({"error": "Cannot remove yourself"},
                               separators=(",", ":")).encode(), status=400)

    cur = db.run(
        """DELETE FROM friend_list
            WHERE (player_id_a=? AND player_id_b=?)
               OR (player_id_a=? AND player_id_b=?)""",
        (me_id, friend_id, friend_id, me_id),
    )
    if not cur.rowcount:
        return _raw(json.dumps({"success": False,
                                "message": "No friendship found to delete"},
                               separators=(",", ":")).encode(), status=404)

    return {
        "success": True,
        "message": "Friend removed successfully",
        "removed_friend_id": friend_id,
    }


# --------------------------------------------------------------------------
# friends: donations  (RECONSTRUCTIONS -- no PHP reference)
# --------------------------------------------------------------------------

def _decode_donation(raw):
    """``users.donation_request`` is JSON text; return an object or ``None``."""
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(str(raw))
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def _my_donation_request(me) -> dict:
    mine = _decode_donation(me.get("donation_request"))
    return {
        "morty_id": (mine or {}).get("morty_id"),
        "next_countdown": 0,
        "donation": None,
    }


@post("/session/friend/donate-morty")
def friend_donate_morty(req):
    """RECONSTRUCTION.  Gift one of my Mortys to an accepted friend.

    Body: ``{session_id, player_id, owned_morty_id}``.  The recipient must
    already be an accepted friend.  The Morty must belong to me and must not be
    locked / trading-locked.  Ownership moves via ``owned_morties.player_id``
    and the Morty is stripped from my decks.

    Invented shape::

        {"success": true,
         "donation": {"player_id": ..., "owned_morty_id": ..., "morty_id": ...},
         "donation_request": <remaining request from that friend or null>}
    """
    body = _body(req)
    me = _auth(req)
    me_id = str(me["player_id"])
    friend_id = body.get("player_id") or req.arg("player_id")
    owned_id = body.get("owned_morty_id") or body.get("morty_id")
    if not friend_id or not owned_id:
        _fail("MISSING_FIELDS", status=400)
    friend_id, owned_id = str(friend_id), str(owned_id)

    if me_id == friend_id:
        _fail("FRIEND_SELF", status=400)
    if not _are_friends(me_id, friend_id):
        _fail("NOT_FRIENDS", status=400)
    if not _user(friend_id):
        _fail("PLAYER_NOT_FOUND", status=404)

    row = _own_morty(me_id, owned_id)
    if row is None:
        _fail("MORTY_NOT_FOUND", status=404)
    if state.truthy(row["is_locked"]) or state.truthy(row["is_trading_locked"]):
        _fail("MORTY_LOCKED", status=409)

    _transfer_morty(owned_id, me_id, friend_id)
    # The request lives on the *requester* (the friend), because
    # ``/session/friend/list`` joins ``p.donation_request`` from the friend's
    # own row -- that is how the donor sees who wants what.  Clear it once the
    # donor hands the requested species over.
    remaining = _decode_donation(
        db.one("SELECT donation_request FROM users WHERE player_id=?",
               (friend_id,))["donation_request"])
    if remaining and (remaining.get("morty_id") in (None, str(row["morty_id"]))
                      or str(remaining.get("player_id") or "") in ("", me_id)):
        db.run("UPDATE users SET donation_request=NULL WHERE player_id=?",
               (friend_id,))
        remaining = None

    return {
        "success": True,
        "donation": {
            "player_id": friend_id,
            "owned_morty_id": owned_id,
            "morty_id": str(row["morty_id"]),
        },
        "donation_request": remaining,
    }


@post("/session/friend/request-morty")
def friend_request_morty(req):
    """RECONSTRUCTION.  Ask a friend to donate a Morty.

    Body: ``{session_id, player_id, morty_id}`` (``morty_id`` is the species id
    the client wants).  The request is stored as JSON in the *requester's*
    ``users.donation_request`` column, because ``/session/friend/list`` surfaces
    each friend's own column -- so the donor sees "this friend wants Morty X"
    next to the right name.  Any previous request is replaced.

    Invented shape::

        {"success": true,
         "donation_request": {"morty_id": ..., "player_id": ..., "_created": ...}}
    """
    body = _body(req)
    me = _auth(req)
    me_id = str(me["player_id"])
    friend_id = body.get("player_id") or req.arg("player_id")
    morty_id = body.get("morty_id")
    if not friend_id or not morty_id:
        _fail("MISSING_FIELDS", status=400)
    friend_id, morty_id = str(friend_id), str(morty_id)

    if me_id == friend_id:
        _fail("FRIEND_SELF", status=400)
    if not _are_friends(me_id, friend_id):
        _fail("NOT_FRIENDS", status=400)

    target = _user(friend_id)
    if not target:
        _fail("PLAYER_NOT_FOUND", status=404)
    owns = db.one("SELECT 1 FROM owned_morties WHERE player_id=? AND morty_id=? LIMIT 1",
                  (friend_id, morty_id))
    if not owns:
        _fail("MORTY_NOT_FOUND", status=404)

    stored = {
        "morty_id": morty_id,
        "player_id": friend_id,
        "username": str(target["username"] or ""),
        "_created": db.iso_now(),
    }
    db.run("UPDATE users SET donation_request=? WHERE player_id=?",
           (json.dumps(stored, separators=(",", ":")), me_id))
    return {"success": True, "donation_request": stored}


# --------------------------------------------------------------------------
# friendships / morty ownership helpers
# --------------------------------------------------------------------------

def _are_friends(a: str, b: str) -> bool:
    row = db.one(
        """SELECT 1 FROM friend_list
            WHERE LOWER(COALESCE(pending,'')) NOT IN ('true','1')
              AND ((player_id_a=? AND player_id_b=?)
                OR (player_id_a=? AND player_id_b=?))
            LIMIT 1""",
        (a, b, b, a),
    )
    return row is not None


def _own_morty(player_id: str, owned_id: str):
    return db.one("SELECT * FROM owned_morties WHERE player_id=? AND owned_morty_id=?",
                  (player_id, owned_id))


def _strip_from_decks(player_id: str, owned_id: str) -> None:
    for deck in db.all_("SELECT deck_id, owned_morty_ids FROM decks WHERE player_id=?",
                        (player_id,)):
        ids = state.decode_ids(deck["owned_morty_ids"])
        if owned_id not in ids:
            continue
        ids = [x for x in ids if x != owned_id]
        db.run("UPDATE decks SET owned_morty_ids=? WHERE player_id=? AND deck_id=?",
               (json.dumps(ids, separators=(",", ":")), player_id, deck["deck_id"]))


def _transfer_morty(owned_id: str, from_id: str, to_id: str) -> None:
    db.run(
        """UPDATE owned_morties
              SET player_id=?, is_trading_locked='false', fight_pit_id=NULL
            WHERE owned_morty_id=?""",
        (to_id, owned_id),
    )
    _strip_from_decks(from_id, owned_id)
    _strip_from_decks(to_id, owned_id)


# --------------------------------------------------------------------------
# /session/emote/room
# --------------------------------------------------------------------------

@post("/session/emote/room")
def emote_room(req):
    """Port of ``session/emote/room/index.php``.

    Emotes are broadcast, not stored: the event goes out as ``emote:room`` with
    ``{"player_id", "emote"}`` and every SSE subscriber in the sender's room
    sees it.  The emote must be 1-24 alphanumerics.
    """
    body = _body(req)
    me = _auth(req)
    emote = str(body.get("emote") or req.arg("emote") or "")
    if not emote:
        return _raw(json.dumps({"error": "Missing session_id or emote"},
                               separators=(",", ":")).encode(), status=400)
    if len(emote) > 24 or not emote.isalnum():
        return _raw(json.dumps({"error": "Invalid emote"},
                               separators=(",", ":")).encode(), status=400)

    player_id = str(me["player_id"])
    room_id = str(me.get("room_id") or "")
    if room_id in ("", "0"):
        return _raw(json.dumps({"error": "Player is not in a room"},
                               separators=(",", ":")).encode(), status=409)

    # NB: SQLite rejects MySQL's ``UPDATE ... LIMIT``; session_id is unique so
    # the LIMIT was redundant anyway.
    db.run("UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE session_id=?",
           (str(me.get("session_id") or ""),))
    events.publish(room_id, "emote:room",
                   {"player_id": player_id, "emote": emote})
    return {"success": True}


# --------------------------------------------------------------------------
# /arena/*
# --------------------------------------------------------------------------

def _col(row, name: str, default=0):
    """Read a column from either a ``sqlite3.Row`` or a plain dict."""
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def _score_of(user) -> int:
    """Arena score proxy.

    The legacy schema has no trophy column, so ``wins`` doubles as the score.
    Every arena handler funnels through here so the proxy can be swapped for a
    real column in one place.
    """
    return int(_col(user, "wins", 0) or 0)


def _rank_of(score: int) -> int:
    rows = db.all_("SELECT COUNT(*) AS n FROM users WHERE COALESCE(wins,0) > ?",
                   (score,))
    return int(rows[0]["n"] if rows else 0) + 1


def _team(player_id: str) -> list[dict]:
    """Active-deck Mortys in the ``[{"id", "variant"}]`` league shape."""
    user = _user(player_id)
    if not user:
        return []
    active = int(user["active_deck_id"] or 0)
    deck = db.one("SELECT owned_morty_ids FROM decks WHERE player_id=? AND deck_id=?",
                  (player_id, active))
    ids = state.decode_ids(deck["owned_morty_ids"]) if deck else []
    if not ids:
        return []
    holes = ",".join("?" * len(ids))
    rows = db.all_(
        f"""SELECT owned_morty_id, morty_id, variant FROM owned_morties
             WHERE owned_morty_id IN ({holes})""",
        ids,
    )
    by_id = {str(r["owned_morty_id"]): r for r in rows}
    return [{"id": str(by_id[oid]["morty_id"]),
             "variant": str(by_id[oid]["variant"] or "Normal")}
            for oid in ids if oid in by_id]


@post("/arena/battle/bot")
def arena_battle_bot(req):
    """Port of ``arena/battle/bot/index.php`` -- RECONSTRUCTED values.

    The PHP ignores its input and echoes one frozen JSON blob.  Every key is
    preserved; the numbers are now derived from the caller when a session is
    supplied, and fall back to the PHP literals otherwise.  No battle is
    created here (the bot battle itself is owned by the battle module).
    """
    me = _maybe_auth(req)
    if me:
        player_id = str(me["player_id"])
        score = _score_of(me)
        rank = _rank_of(score)
        return {
            "event_id": ARENA_EVENT_ID,
            "shard_id": ARENA_SHARD_ID,
            "target_trophy_count": score + 25,
            "attacker_score": score,
            "defender_score": max(0, score - 25),
            "energy": ENERGY_AMOUNT,
            "used_energy": True,
            "attacker_rank": rank,
            "found_opponent": True,
            "is_real_opponent": False,
        }
    return {
        "event_id": ARENA_EVENT_ID,
        "shard_id": ARENA_SHARD_ID,
        "target_trophy_count": 79,
        "attacker_score": 100,
        "defender_score": 75,
        "energy": ENERGY_AMOUNT,
        "used_energy": True,
        "attacker_rank": 548,
        "found_opponent": True,
        "is_real_opponent": False,
    }


@post("/arena/energy/fetch")
def arena_energy_fetch(req):
    """Port of ``arena/energy/fetch/index.php`` (a two-key static echo)."""
    return {"energy_amount": ENERGY_AMOUNT, "energy_time": None}


@post("/arena/events/list")
def arena_events_list(req):
    """Port of ``arena/events/list/index.php``.

    The PHP emits exactly one active event with fixed countdowns.  All keys are
    preserved; ``data_time`` is stamped server-side so the client's clock skew
    maths stays sane.
    """
    return {
        "events": [{
            "event_id": ARENA_EVENT_ID,
            "current_phase": "active",
            "reward_time_remaining": 318892832,
            "phase_time_remaining": 316792832,
            "data_time": db.iso_now(),
            "starting_energy": ENERGY_AMOUNT,
            "energy_refill_time_minutes": 10,
            "battle_energy_cost": 0,
            "battle_premium_energy_cost": 0,
            "max_regen_energy": ENERGY_AMOUNT,
            "maintenance": False,
        }],
    }


# (rewards_name, bracket_start, bracket_end, base_quantity, base_type,
#  sort_order, ids) -- copied verbatim from arena/rewards/list/index.php.
_REWARD_ROWS = [
    ("REWARDS_LIVE_Through_Ages_Morty", 11, 50, 1, "MORTY", 1, ["MortyBronzeAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 201, 400, 1, "MORTY", 1, ["MortyStoneAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 651, 1000, 1, "COUPON", 1, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 401, 650, 1, "MORTY", 1, ["MortyStoneAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 101, 200, 1, "MORTY", 1, ["MortyStoneAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 1001, 1500, 1, "ITEM", 1, ["ItemSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 51, 100, 1, "MORTY", 1, ["MortyBronzeAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 1, 1, 1, "MORTY", 1, ["MortyIronAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 3, 3, 1, "MORTY", 1, ["MortyIronAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 1501, 2000, 1, "ITEM", 1, ["ItemSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 2, 2, 1, "MORTY", 1, ["MortyIronAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 4, 10, 1, "MORTY", 1, ["MortyIronAge"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 3, 3, 4, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 101, 200, 2, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 201, 400, 2, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 2, 2, 5, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 401, 650, 3, "ITEM", 2, ["ItemSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 401, 650, 1, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 4, 10, 3, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 51, 100, 2, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 1, 1, 7, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 651, 1000, 2, "ITEM", 2, ["ItemSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 11, 50, 3, "COUPON", 2, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 101, 200, 2, "ITEM", 2, ["ItemGreatSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 1, 1, 1, "ITEM", 3, ["ItemSensationalSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 101, 200, 1, "ITEM", 3, ["ItemMegaSeedSpeed"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 11, 50, 3, "ITEM", 3, ["ItemGreatSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 3, 3, 1, "ITEM", 3, ["ItemSensationalSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 51, 100, 2, "ITEM", 3, ["ItemGreatSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 2, 2, 1, "ITEM", 3, ["ItemSensationalSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 201, 400, 2, "ITEM", 3, ["ItemGreatSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 4, 10, 1, "ITEM", 4, ["ItemSensationalSerum"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 2, 2, 3, "ITEM", 4, ["ItemMegaSeedSpeed"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 3, 3, 3, "ITEM", 4, ["ItemMegaSeedSpeed"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 51, 100, 1, "ITEM", 4, ["ItemMegaSeedSpeed"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 1, 1, 4, "ITEM", 4, ["ItemMegaSeedSpeed"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 3, 3, 1, "ITEM", 5, ["ItemMegaSeedAttack"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 2, 2, 1, "ITEM", 5, ["ItemMegaSeedAttack"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 11, 50, 1, "ITEM", 5, ["ItemMegaSeedSpeed"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 1, 1, 2, "ITEM", 5, ["ItemMegaSeedAttack"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 4, 10, 2, "ITEM", 5, ["ItemMegaSeedSpeed"]),
    ("REWARDS_LIVE_Through_Ages_Morty", 101, 200, 900, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 4, 10, 1300, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 201, 400, 800, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 3501, 8000, 100, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 651, 1000, 600, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 2, 2, 1750, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 51, 100, 1000, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 11, 50, 1100, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 2501, 3500, 150, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 1501, 2000, 400, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 1, 1, 2000, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 2001, 2500, 200, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 1001, 1500, 500, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 401, 650, 700, "COIN", 1000, None),
    ("REWARDS_LIVE_Through_Ages_Morty", 3, 3, 1500, "COIN", 1000, None),
]


def _reward(row) -> dict:
    name, start, end, quantity, kind, order, ids = row
    return {
        "rewards_name": name,
        "bracket_start": start,
        "bracket_end": end,
        "base_quantity": quantity,
        "base_type": kind,
        "sort_order": order,
        "base_parameters": {"ids": list(ids)} if ids else None,
    }


@post("/arena/rewards/list")
def arena_rewards_list(req):
    """Port of ``arena/rewards/list/index.php`` (56 hard-coded brackets)."""
    return {"rewards": [_reward(row) for row in _REWARD_ROWS]}


@post("/arena/rewards/pending/list")
def arena_rewards_pending_list(req):
    """Port of ``arena/rewards/pending/list/index.php`` (an empty ``result``)."""
    return {"result": []}


def _league_row(user, rank: int, now: str) -> dict:
    player_id = str(user["player_id"])
    return {
        "player_id": player_id,
        "username": str(user["username"] or ""),
        "last_battle_time": _iso(user["last_seen"]),
        "avatar": str(user["player_avatar_id"] or "AvatarRickDefault"),
        "level": int(user["level"] or 1),
        "event_id": ARENA_SCORES_ID,
        "shard_id": ARENA_SHARD_ID,
        "score": _score_of(user),
        "streak": int(user["streak"] or 0),
        "current_time": now,
        "reward_rank": rank,
        "team_details": [],
        "team": _team(player_id),
        "rank_id": rank,
        "index": rank - 1,
    }


@post("/arena/scores/list")
def arena_scores_list(req):
    """Port of ``arena/scores/list/index.php``.

    The PHP echoes a frozen 11-row league table (``id``, ``results``,
    ``promotion_trophy_amount``, ``relegation_trophy_amount``).  Every key is
    preserved; the rows are now the top 11 players by arena score, with
    ``is_player``-style identification left to the client (the PHP has no such
    key here).
    """
    now = db.iso_now()
    rows = db.all_(
        """SELECT * FROM users
            ORDER BY COALESCE(wins,0) DESC, COALESCE(level,1) DESC
            LIMIT 11"""
    )
    return {
        "id": ARENA_SCORES_ID,
        "results": [_league_row(r, i + 1, now) for i, r in enumerate(rows)],
        "promotion_trophy_amount": ARENA_PROMOTION_TROPHIES,
        "relegation_trophy_amount": ARENA_RELEGATION_TROPHIES,
    }


# --------------------------------------------------------------------------
# /shard/*
# --------------------------------------------------------------------------

@post("/shard/scores")
def shard_scores(req):
    """Port of ``shard/scores/index.php``.

    The PHP echoes a frozen leaderboard under ``{"event_id", "results"}`` with
    ``rank_id`` as a *string*, ``is_player`` as the strings ``"true"``/
    ``"false"``, and ``score`` always 0.  The keys are preserved verbatim; the
    rows are now live (ordered by damage proxy ``wins``, ``score`` still 0
    because the legacy schema stores no shard damage).
    """
    me = _maybe_auth(req)
    me_id = str(me["player_id"]) if me else None
    rows = db.all_(
        """SELECT * FROM users
            ORDER BY COALESCE(wins,0) DESC, COALESCE(level,1) DESC
            LIMIT 12"""
    )
    return {
        "event_id": RAID_EVENT_ID,
        "results": [
            {
                "rank_id": str(i + 1),
                "username": str(r["username"] or ""),
                "avatar": str(r["player_avatar_id"] or "AvatarRickDefault"),
                "score": 0,
                "is_player": "true" if str(r["player_id"]) == me_id else "false",
            }
            for i, r in enumerate(rows)
        ],
    }


@post("/shard/health")
def shard_health(req):
    """RECONSTRUCTION.  Shard liveness probe for the client's retry logic.

    Invented shape::

        {"healthy": true, "status": "ok", "shard_id": 3,
         "event_id": "RaidBossKillerAsteroid_2025",
         "server_time": "...", "queue_depth": <pending event rows>}
    """
    depth = db.one("SELECT COUNT(*) AS n FROM event_queue")
    return {
        "healthy": True,
        "status": "ok",
        "shard_id": ARENA_SHARD_ID,
        "event_id": RAID_EVENT_ID,
        "server_time": db.iso_now(),
        "queue_depth": int(depth["n"] if depth else 0),
    }


# --------------------------------------------------------------------------
# /trading/*
# --------------------------------------------------------------------------

_TRADE_TABLE_READY = False


def _ensure_trades() -> None:
    """Create the ``trades`` table on first use.

    The legacy schema has no trading state; ``direction`` records whether the
    row is an offer (A gives ``morty_a``) or a request (A wants ``morty_a``
    from B).  ``morty_a``/``morty_b`` hold ``owned_morty_id`` values.
    """
    global _TRADE_TABLE_READY
    if _TRADE_TABLE_READY:
        return
    db.run(
        """CREATE TABLE IF NOT EXISTS trades (
               trade_id    TEXT PRIMARY KEY,
               player_id_a TEXT NOT NULL,
               player_id_b TEXT NOT NULL,
               morty_a     TEXT,
               morty_b     TEXT,
               direction   TEXT NOT NULL DEFAULT 'offer',
               status      TEXT NOT NULL DEFAULT 'pending',
               created     TEXT NOT NULL,
               modified    TEXT NOT NULL
           )"""
    )
    _TRADE_TABLE_READY = True


@_claim("POST", "/trading/config")
def trading_config(req):
    """Port of ``trading/config/index.php`` (a static four-key echo).

    Overrides the unfaithful placeholder registered by
    :mod:`pmnet.routes.session`; see ``_claim``.
    """
    return {
        "config_id": "DEFAULT",
        "premium_trade_cost": TRADE_PREMIUM_COST,
        "max_trades": TRADE_MAX,
        "trade_cooldown_minutes": TRADE_COOLDOWN_MINUTES,
    }


def _trade_json(row, viewer_id: str) -> dict:
    other_id = (str(row["player_id_b"]) if str(row["player_id_a"]) == viewer_id
                else str(row["player_id_a"]))
    other = _user(other_id)
    owned_id = str(row["morty_a"] or "")
    morty = db.one("SELECT morty_id, variant, level FROM owned_morties WHERE owned_morty_id=?",
                   (owned_id,)) if owned_id else None
    return {
        "trade_id": str(row["trade_id"]),
        "player_id": other_id,
        "username": str(other["username"]) if other else "",
        "player_avatar_id": str(other["player_avatar_id"]) if other else "AvatarRickDefault",
        "level": int(other["level"] or 1) if other else 1,
        "owned_morty_id": owned_id or None,
        "morty_id": str(morty["morty_id"]) if morty else None,
        "variant": str(morty["variant"] or "Normal") if morty else None,
        "morty_level": int(morty["level"] or 1) if morty else None,
        "direction": str(row["direction"]),
        "is_sender": str(row["player_id_a"]) == viewer_id,
        "status": str(row["status"]),
        "_created": _iso(row["created"]),
        "_modified": _iso(row["modified"]),
    }


@post("/trading/get-trades")
def trading_get_trades(req):
    """RECONSTRUCTION.  Every pending trade the caller is part of.

    Invented shape::

        {"trades": [ <see _trade_json> ], "max_trades": 3,
         "premium_trade_cost": 3, "trade_cooldown_minutes": 1440,
         "poll_period": 5}
    """
    _ensure_trades()
    me = _auth(req)
    me_id = str(me["player_id"])
    rows = db.all_(
        """SELECT * FROM trades
            WHERE (player_id_a=? OR player_id_b=?)
              AND status='pending'
            ORDER BY created DESC""",
        (me_id, me_id),
    )
    return {
        "trades": [_trade_json(r, me_id) for r in rows],
        "max_trades": TRADE_MAX,
        "premium_trade_cost": TRADE_PREMIUM_COST,
        "trade_cooldown_minutes": TRADE_COOLDOWN_MINUTES,
        "poll_period": FRIEND_POLL_PERIOD,
    }


def _publish_trade(user, event: str, payload: dict) -> None:
    room_id = str(user.get("room_id") or "")
    if room_id and room_id != "0":
        events.publish(room_id, event, payload)


@post("/trading/offer-morty")
def trading_offer_morty(req):
    """RECONSTRUCTION.  Offer one of my Mortys to another player.

    Body: ``{session_id, player_id, owned_morty_id}``.  Re-offering to the same
    player replaces the outstanding offer rather than stacking.  The Morty is
    trading-locked while the offer is open.

    Invented response: the ``_trade_json`` object plus ``{"success": true}``.
    """
    _ensure_trades()
    me = _auth(req)
    me_id = str(me["player_id"])
    body = _body(req)
    target_id = body.get("player_id") or req.arg("player_id")
    owned_id = body.get("owned_morty_id") or body.get("morty_id")
    if not target_id or not owned_id:
        _fail("MISSING_FIELDS", status=400)
    target_id, owned_id = str(target_id), str(owned_id)

    if me_id == target_id:
        _fail("TRADE_SELF", status=400)
    if not _user(target_id):
        _fail("PLAYER_NOT_FOUND", status=404)
    row = _own_morty(me_id, owned_id)
    if row is None:
        _fail("MORTY_NOT_FOUND", status=404)
    if state.truthy(row["is_locked"]) or state.truthy(row["is_trading_locked"]):
        _fail("MORTY_LOCKED", status=409)

    open_trades = db.all_(
        """SELECT * FROM trades
            WHERE status='pending'
              AND ((player_id_a=? AND player_id_b=?) OR (player_id_a=? AND player_id_b=?))""",
        (me_id, target_id, target_id, me_id),
    )
    if len(open_trades) >= TRADE_MAX:
        _fail("TOO_MANY_TRADES", status=409)

    now = db.iso_now()
    existing = next((r for r in open_trades
                     if str(r["player_id_a"]) == me_id
                     and str(r["direction"]) == "offer"), None)
    if existing:
        db.run("UPDATE trades SET morty_a=?, modified=? WHERE trade_id=?",
               (owned_id, now, str(existing["trade_id"])))
        trade_id = str(existing["trade_id"])
    else:
        trade_id = _uuid()
        db.run(
            """INSERT INTO trades
                   (trade_id, player_id_a, player_id_b, morty_a, morty_b,
                    direction, status, created, modified)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (trade_id, me_id, target_id, owned_id, None, "offer", "pending",
             now, now),
        )

    db.run("UPDATE owned_morties SET is_trading_locked='true' WHERE owned_morty_id=?",
           (owned_id,))
    trade = _trade_json(
        db.one("SELECT * FROM trades WHERE trade_id=?", (trade_id,)), me_id)
    _publish_trade(me, "trading:trade-updated", trade)
    _publish_trade(db.rowdict(_user(target_id)) or {},
                   "trading:trade-updated", trade)
    return {"success": True, "trade": trade}


@post("/trading/offer-response")
def trading_offer_response(req):
    """RECONSTRUCTION.  Accept or decline an offer/request aimed at me.

    Body: ``{session_id, trade_id, accept}``.  On acceptance the offered Morty
    changes owner (``owned_morties.player_id``), the trading lock is cleared and
    the row is marked ``accepted``; otherwise it is marked ``declined``.

    Invented shape: ``{"success": true, "status": "accepted"|"declined",
    "trade": {...}}``.
    """
    _ensure_trades()
    me = _auth(req)
    me_id = str(me["player_id"])
    body = _body(req)
    trade_id = body.get("trade_id") or req.arg("trade_id")
    accept = body.get("accept")
    if not trade_id or not isinstance(accept, bool):
        _fail("MISSING_FIELDS", status=400)

    row = db.one("SELECT * FROM trades WHERE trade_id=?", (str(trade_id),))
    if row is None:
        _fail("TRADE_NOT_FOUND", status=404)
    if me_id not in (str(row["player_id_a"]), str(row["player_id_b"])):
        _fail("NOT_A_PARTICIPANT", status=403)
    if str(row["status"]) != "pending":
        _fail("TRADE_NOT_PENDING", status=409)

    other_id = (str(row["player_id_b"]) if str(row["player_id_a"]) == me_id
                else str(row["player_id_a"]))
    now = db.iso_now()

    if accept:
        owned_id = str(row["morty_a"] or "")
        if owned_id:
            _transfer_morty(owned_id, other_id, me_id)
        db.run("UPDATE trades SET status='accepted', modified=? WHERE trade_id=?",
               (now, str(row["trade_id"])))
    else:
        owned_id = str(row["morty_a"] or "")
        if owned_id:
            db.run("UPDATE owned_morties SET is_trading_locked='false' "
                   "WHERE owned_morty_id=?", (owned_id,))
        db.run("UPDATE trades SET status='declined', modified=? WHERE trade_id=?",
               (now, str(row["trade_id"])))

    trade = _trade_json(db.one("SELECT * FROM trades WHERE trade_id=?",
                               (str(row["trade_id"]),)), me_id)
    _publish_trade(me, "trading:trade-updated", trade)
    _publish_trade(db.rowdict(_user(other_id)) or {},
                   "trading:trade-updated", trade)
    return {"success": True, "status": str(trade["status"]), "trade": trade}


@post("/trading/request-morty")
def trading_request_morty(req):
    """RECONSTRUCTION.  Ask another player for one of their Mortys.

    Body: ``{session_id, player_id, owned_morty_id}``.  Creates a
    ``direction='request'`` trade row pointing at the target's Morty.  The
    target's Morty is *not* locked (they may refuse, and the client must keep
    showing it as tradeable).

    Invented response: ``{"success": true, "trade": {...}}``.
    """
    _ensure_trades()
    me = _auth(req)
    me_id = str(me["player_id"])
    body = _body(req)
    target_id = body.get("player_id") or req.arg("player_id")
    owned_id = body.get("owned_morty_id") or body.get("morty_id")
    if not target_id or not owned_id:
        _fail("MISSING_FIELDS", status=400)
    target_id, owned_id = str(target_id), str(owned_id)

    if me_id == target_id:
        _fail("TRADE_SELF", status=400)
    if not _user(target_id):
        _fail("PLAYER_NOT_FOUND", status=404)
    if _own_morty(target_id, owned_id) is None:
        _fail("MORTY_NOT_FOUND", status=404)

    open_trades = db.all_(
        """SELECT * FROM trades
            WHERE status='pending'
              AND ((player_id_a=? AND player_id_b=?) OR (player_id_a=? AND player_id_b=?))""",
        (me_id, target_id, target_id, me_id),
    )
    if len(open_trades) >= TRADE_MAX:
        _fail("TOO_MANY_TRADES", status=409)

    now = db.iso_now()
    trade_id = _uuid()
    db.run(
        """INSERT INTO trades
               (trade_id, player_id_a, player_id_b, morty_a, morty_b,
                direction, status, created, modified)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (trade_id, me_id, target_id, owned_id, None, "request", "pending",
         now, now),
    )
    trade = _trade_json(db.one("SELECT * FROM trades WHERE trade_id=?", (trade_id,)),
                        me_id)
    _publish_trade(me, "trading:trade-updated", trade)
    _publish_trade(db.rowdict(_user(target_id)) or {},
                   "trading:trade-updated", trade)
    return {"success": True, "trade": trade}


@post("/trading/cancel-trade")
def trading_cancel_trade(req):
    """RECONSTRUCTION.  Cancel a pending trade by id.

    Body: ``{session_id, trade_id}``.  Either participant may cancel; the row
    is deleted and any trading lock released.  Invented shape:
    ``{"success": true, "trade_id": ...}``.
    """
    _ensure_trades()
    me = _auth(req)
    me_id = str(me["player_id"])
    trade_id = _body(req).get("trade_id") or req.arg("trade_id")
    if not trade_id:
        _fail("MISSING_FIELDS", status=400)

    row = db.one("SELECT * FROM trades WHERE trade_id=?", (str(trade_id),))
    if row is None:
        _fail("TRADE_NOT_FOUND", status=404)
    if me_id not in (str(row["player_id_a"]), str(row["player_id_b"])):
        _fail("NOT_A_PARTICIPANT", status=403)

    if str(row["morty_a"] or ""):
        db.run("UPDATE owned_morties SET is_trading_locked='false' "
               "WHERE owned_morty_id=?", (str(row["morty_a"]),))
    db.run("DELETE FROM trades WHERE trade_id=?", (str(row["trade_id"]),))
    return {"success": True, "trade_id": str(row["trade_id"])}


@post("/trading/cancel-trade-offer")
def trading_cancel_trade_offer(req):
    """RECONSTRUCTION.  Withdraw my outstanding offer to one player.

    Body: ``{session_id, player_id}`` (optionally ``trade_id``).  Cancels every
    pending trade *I* initiated with that player.  Invented shape:
    ``{"success": true, "cancelled": [trade_id, ...]}``.
    """
    _ensure_trades()
    me = _auth(req)
    me_id = str(me["player_id"])
    body = _body(req)
    trade_id = body.get("trade_id")
    target_id = body.get("player_id") or req.arg("player_id")
    if not trade_id and not target_id:
        _fail("MISSING_FIELDS", status=400)

    if trade_id:
        rows = db.all_(
            "SELECT * FROM trades WHERE trade_id=? AND player_id_a=?",
            (str(trade_id), me_id),
        )
    else:
        rows = db.all_(
            """SELECT * FROM trades
                WHERE player_id_a=? AND player_id_b=? AND status='pending'""",
            (me_id, str(target_id)),
        )

    cancelled = []
    for row in rows:
        if str(row["morty_a"] or ""):
            db.run("UPDATE owned_morties SET is_trading_locked='false' "
                   "WHERE owned_morty_id=?", (str(row["morty_a"]),))
        db.run("DELETE FROM trades WHERE trade_id=?", (str(row["trade_id"]),))
        cancelled.append(str(row["trade_id"]))
    return {"success": True, "cancelled": cancelled}
