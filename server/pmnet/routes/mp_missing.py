"""Endpoints the client calls that the reconstruction had not covered.

Derived by diffing every ``EP_*`` constant in ``NetworkDefs`` against the
registered routes, then reading the matching ``*Response`` class out of the
IL2CPP dump for the exact field names.  Field names matter: the client
deserialises into a concrete class, so ``invite_id`` where it expects
``battle_invite_id`` deserialises to null and the multiplayer lobby reports
"Unable to find manifest resource." instead of opening.

Reference shapes (dump.cs, TypeDefIndex shown per handler below).
"""

from __future__ import annotations

import time
import uuid as _uuid

from .. import db, state
from ..http import get, post, require_player


def _uid() -> str:
    return str(_uuid.uuid4())


def _has_table(name: str) -> bool:
    try:
        return db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)) is not None
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# multiplayer battle invites -- this is the multiplayer entry path
# --------------------------------------------------------------------------

@post("/session/battle-invite/create")
def battle_invite_create(req):
    """TypeDefIndex 490 ``SendInviteResponse``.

        string battle_invite_id
        string battle_type
        float  battle_invite_ttl
        string token
    """
    player = require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    invite_id = _uid()
    if _has_table("battle_invites"):
        db.run("""INSERT INTO battle_invites
                      (invite_id, from_player_id, to_player_id, status)
                  VALUES (?,?,?,?)""",
               (invite_id, player["player_id"],
                body.get("player_id") or body.get("target_player_id") or player["player_id"],
                "pending"))
    return {
        "battle_invite_id": invite_id,
        "battle_type": body.get("battle_type") or "multiplayer",
        "battle_invite_ttl": 30.0,
        "token": invite_id,
    }


@post("/session/battle-invite/response")
def battle_invite_response(req):
    """TypeDefIndex 493 ``InviteAcceptedResponse`` / 492 ``InviteDeclinedResponse``.

        string battle_invite_id
        string battle_type          (accepted only)
    """
    require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    invite_id = body.get("battle_invite_id") or body.get("invite_id") or _uid()
    accepted = bool(body.get("accepted", True))
    if _has_table("battle_invites"):
        db.run("UPDATE battle_invites SET status=? WHERE invite_id=?",
               ("accepted" if accepted else "declined", invite_id))
    if accepted:
        return {"battle_invite_id": invite_id,
                "battle_type": body.get("battle_type") or "multiplayer"}
    return {"battle_invite_id": invite_id}


@post("/session/battle-invite/revoke")
def battle_invite_revoke(req):
    """TypeDefIndex 494 ``InviteRevokedResponse`` -- just ``battle_invite_id``."""
    require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    invite_id = body.get("battle_invite_id") or body.get("invite_id") or ""
    if _has_table("battle_invites") and invite_id:
        db.run("UPDATE battle_invites SET status=? WHERE invite_id=?", ("revoked", invite_id))
    return {"battle_invite_id": invite_id}


# --------------------------------------------------------------------------
# arena matchmaking / custom battle
# --------------------------------------------------------------------------

@post("/arena/matchmaking")
def arena_matchmaking(req):
    """TypeDefIndex 495 ``ArenaMatchmakingResponse``.

    With no other players online every field still has to be present, otherwise
    the lobby cannot render a bracket.
    """
    player = require_player(req)
    return {
        "event_id": "",
        "shard_id": 3,
        "target_trophy_count": int(player.get("wins") or 0),
        "attacker_score": int(player.get("wins") or 0),
        "defender_score": 0,
        "found_opponent": False,
        "used_energy": False,
        "energy": 100,
        "attacker_rank": 1,
    }


@post("/session/battle-custom-battle")
@post("/session/battle-bot-custom")
def battle_custom(req):
    """TypeDefIndex 469 ``BattleStartResponse``: battle_id + battle_type."""
    require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    return {
        "battle_id": _uid(),
        "battle_type": body.get("battle_type") or "bot",
    }


# --------------------------------------------------------------------------
# emotes
# --------------------------------------------------------------------------

@post("/session/emote/p2p")
def emote_p2p(req):
    require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    return {"emote_id": body.get("emote_id") or body.get("emote") or ""}


# --------------------------------------------------------------------------
# morty slots
# --------------------------------------------------------------------------

@post("/session/morty/slots/buy")
def morty_slots_buy(req):
    player = require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    count = int(body.get("count") or body.get("slots") or 1)
    try:
        db.run("UPDATE users SET morty_slots = COALESCE(morty_slots, 200) + ? "
               "WHERE player_id = ?", (count, player["player_id"]))
    except Exception:  # noqa: BLE001
        pass
    return _details(player["player_id"])


@post("/session/morty/slots/ftue")
def morty_slots_ftue(req):
    player = require_player(req)
    return _details(player["player_id"])


def _details(player_id: str):
    return state.player_details(db.rowdict(
        db.one("SELECT * FROM users WHERE player_id=?", (player_id,))))


# --------------------------------------------------------------------------
# rewards / claims
# --------------------------------------------------------------------------

@post("/reward/single-player")
def reward_sp(req):
    require_player(req)
    return {"rewards": []}


@post("/reward/multiplayer")
def reward_mp(req):
    require_player(req)
    return {"rewards": []}


@post("/session/challenge/claim-reward")
def challenge_claim(req):
    require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    return {"challenge_id": body.get("challenge_id") or "", "rewards": []}


@post("/arena/rewards/claim")
def arena_claim(req):
    require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    return {"reward_id": body.get("reward_id") or "", "rewards": []}


# --------------------------------------------------------------------------
# events / misc
# --------------------------------------------------------------------------

@post("/event/claimRewards")
def event_claim_rewards(req):
    require_player(req)
    body = req.json if isinstance(req.json, dict) else {}
    return {"event_id": body.get("event_id") or "", "rewards": []}


@post("/lte/fyre/events/playerinfo")
def fyre_player_info(req):
    player = require_player(req)
    return {"player_id": player["player_id"], "event_id": "", "active": False,
            "phase": "", "score": 0}


@post("/iap")
def iap(req):
    return {"products": []}
