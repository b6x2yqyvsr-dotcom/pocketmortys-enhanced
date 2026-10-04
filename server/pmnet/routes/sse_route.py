"""GET /sse -- the long-lived event stream.

Lifecycle, mirroring the reference implementation:

1. Client presents the JWT it got from /user/login.
2. We immediately emit ``session:start`` carrying the world table and ping
   config.  The client will not proceed past the loading screen without it.
3. We idle until /session/join-room sets ``users.room_id``; only then does the
   client expect room traffic.
4. On a fresh connect we replay a room snapshot (other players, live pickups,
   roaming wild Mortys, bots).  On a resume (client sent Last-Event-ID) we skip
   the snapshot and replay from the cursor instead.
5. Steady state: poll the event queue above our cursor and push deltas.
"""

from __future__ import annotations

import json
import time

from .. import config, db, events
from ..http import SSEStream, get, json_response
from ..jwtutil import signer


def _frame(event: str, payload) -> str:
    data = payload if isinstance(payload, str) else json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def _session_start(user: dict, session_id: str) -> dict:
    return {
        "player_id": user["player_id"],
        "session_id": session_id,
        "username": user["username"],
        "level": int(user["level"] or 1),
        "tags": [],
        "ping_interval": config.PING_INTERVAL_SECONDS,
        "ping_url": f"{config.public_base()}/session/ping-dynamic",
        "keep_alive": config.SSE_KEEPALIVE_SECONDS,
        "server_instance": "/pmnet/1/1",
        "worlds": config.WORLDS,
        "owned_morty_limit": config.OWNED_MORTY_LIMIT,
    }


@get("/sse")
def sse(req):
    token = req.arg("token") or ""
    payload = signer.decode(token, verify=True) if token else None
    if not payload:
        return json_response({"error": "Missing session_id"}, status=400)

    session_id = str(payload.get("session_id") or "")
    user = db.rowdict(db.one(
        """SELECT player_id, username, level, room_id, state,
                  COALESCE(last_event_id,0) AS last_event_id
             FROM users WHERE session_id = ? LIMIT 1""",
        (session_id,),
    ))
    if not user:
        return json_response({"error": "Not authenticated"}, status=401)

    player_id = str(user["player_id"])

    # Resume support: EventSource sends Last-Event-ID, and the reference also
    # accepted ?since=.
    resume_from = None
    header_last = req.header("last-event-id").strip()
    if header_last.isdigit():
        resume_from = int(header_last)
    elif (req.arg("since") or "").isdigit():
        resume_from = int(req.arg("since"))

    def gen():
        yield _frame("session:start", _session_start(user, session_id))

        # ---- wait for a room ----
        room_id = ""
        last_keepalive = time.time()
        while True:
            row = db.one("SELECT room_id FROM users WHERE player_id = ? LIMIT 1",
                         (player_id,))
            room_id = str(row["room_id"] or "") if row else ""
            if room_id and room_id != "0":
                break
            if time.time() - last_keepalive >= 25:
                yield _frame("session:keep-alive", "0")
                last_keepalive = time.time()
            time.sleep(0.25)

        # ---- snapshot or resume ----
        if resume_from is None:
            yield from _snapshot(room_id, player_id)
            cursor = events.room_max_id(room_id)
        else:
            cursor = resume_from
        events.cursor_set(player_id, cursor)

        # ---- steady state ----
        last_keepalive = time.time()
        while True:
            db.run("UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE player_id=?",
                   (player_id,))
            sent = False
            try:
                rows = events.since(room_id, cursor)
            except Exception:  # noqa: BLE001
                rows = []

            for row in rows:
                eid = int(row["id"])
                name = str(row["event_name"])
                body = str(row["payload_json"])
                target = str(row["target_player_id"] or "")

                if name.startswith(events.PRIVATE_PREFIX):
                    if target and target != player_id:
                        cursor = eid
                        continue
                    if not target and not events.payload_involves(body, player_id):
                        cursor = eid
                        continue

                if name == "room:wild-morty-added":
                    body = events.normalize_wild_morty(body)

                yield _frame(name, body)
                cursor = eid
                sent = True

            events.cursor_set(player_id, cursor)

            if not sent and time.time() - last_keepalive >= config.SSE_KEEPALIVE_SECONDS:
                yield _frame("session:keep-alive", "0")
                last_keepalive = time.time()

            time.sleep(0.3)

    return SSEStream(gen())


def _snapshot(room_id: str, player_id: str):
    """Replay the live contents of a room to a freshly connected client."""

    # other players present in the last 5 minutes
    stale = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 300))
    for row in db.all_(
        """SELECT player_id, username, player_avatar_id, level, state
             FROM users
            WHERE room_id = ? AND COALESCE(last_seen,'') >= ?""",
        (room_id, stale),
    ):
        if str(row["player_id"]) == player_id:
            continue
        yield _frame("room:user-added", {
            "player_id": str(row["player_id"]),
            "username": str(row["username"] or ""),
            "player_avatar_id": str(row["player_avatar_id"] or "AvatarRickDefault"),
            "level": int(row["level"] or 1),
            "state": str(row["state"] or "WORLD"),
        })

    # pickups that nobody has collected yet
    for row in db.all_(
        """SELECT id, payload_json FROM event_queue
            WHERE room_id = ? AND event_name = 'room:pickup-added'
              AND pickup_id_collected_by_player_id IS NULL
            ORDER BY id ASC""",
        (room_id,),
    ):
        yield _frame("room:pickup-added", str(row["payload_json"]))

    # wild mortys (payload normalised so they are actually interactable)
    for row in db.all_(
        """SELECT id, payload_json FROM event_queue
            WHERE room_id = ? AND event_name = 'room:wild-morty-added'
            ORDER BY id ASC""",
        (room_id,),
    ):
        yield _frame("room:wild-morty-added",
                     events.normalize_wild_morty(str(row["payload_json"])))

    # bots
    for row in db.all_(
        """SELECT id, payload_json FROM event_queue
            WHERE room_id = ? AND event_name = 'room:bot-added'
            ORDER BY id ASC""",
        (room_id,),
    ):
        yield _frame("room:bot-added", str(row["payload_json"]))
