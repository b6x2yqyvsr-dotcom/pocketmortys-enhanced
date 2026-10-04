"""Web control panel for the private server.

Serves a single-page dashboard at ``/admin`` plus the JSON API it talks to.
The layout borrows from KLink's control panel: a dark sidebar, a live status
header and one page per concern (overview, players, rooms, events, config,
console).

Nothing here is reachable from the game client -- the panel lives under
``/admin`` and its API under ``/admin/api``.  The API is read-mostly; the few
mutating calls (kick, grant, cheat toggles) are the ones an operator needs
while the server is running.
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

from .. import config, db, state
from ..http import Response, get, post, route

PANEL_DIR = Path(__file__).resolve().parent.parent / "panel"

_STARTED_AT = time.time()


# --------------------------------------------------------------------------
# static panel
# --------------------------------------------------------------------------

def _asset(name: str, content_type: str) -> Response:
    path = (PANEL_DIR / name).resolve()
    try:
        path.relative_to(PANEL_DIR.resolve())
    except ValueError:
        return Response(status=404, body=b"not found")
    if not path.is_file():
        return Response(status=404, body=b"not found")
    return Response(status=200, body=path.read_bytes(),
                    content_type=content_type, headers={"Cache-Control": "no-cache"})


@get("/admin")
def panel_index(req):
    return _asset("index.html", "text/html; charset=utf-8")


@get("/admin/style.css")
def panel_css(req):
    return _asset("style.css", "text/css; charset=utf-8")


@get("/admin/app.js")
def panel_js(req):
    return _asset("app.js", "application/javascript; charset=utf-8")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _q(req, name: str, default: str = "") -> str:
    """``req.query`` is parse_qs-shaped: values arrive as lists."""
    v = req.query.get(name)
    if v is None:
        return default
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else default
    return str(v)


def _qi(req, name: str, default: int) -> int:
    try:
        return int(_q(req, name) or default)
    except (TypeError, ValueError):
        return default


def _one(sql, params=()):
    try:
        row = db.one(sql, params)
        return db.rowdict(row) if row is not None else None
    except Exception:  # noqa: BLE001
        return None


def _all(sql, params=()):
    try:
        return db.rowdicts(db.all_(sql, params))
    except Exception:  # noqa: BLE001
        return []


def _count(table: str, where: str = "", params=()) -> int:
    row = _one(f"SELECT COUNT(*) AS n FROM {table} {where}", params)
    return int(row["n"]) if row else 0


def _table_exists(name: str) -> bool:
    return _one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def _seconds_since(stamp) -> float | None:
    """``users.last_seen`` is an ISO string in this schema, but older rows and
    the legacy import carry epoch ints.  Accept both."""
    if stamp in (None, ""):
        return None
    if isinstance(stamp, (int, float)):
        return time.time() - float(stamp)
    text = str(stamp).strip()
    if text.isdigit():
        return time.time() - float(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            from datetime import datetime
            return (datetime.utcnow() - datetime.strptime(text[:26], fmt)).total_seconds()
        except ValueError:
            continue
    return None


def _is_online(stamp, window: int = 300) -> bool:
    delta = _seconds_since(stamp)
    return delta is not None and 0 <= delta < window


def _cheats() -> dict:
    raw = os.environ.get("PMNET_CHEAT", "all").strip()
    items = {x.strip() for x in raw.replace(",", " ").split() if x.strip()}
    on = "all" in items or "1" in items or "true" in items.lower()
    return {
        "enabled": on or bool(items),
        "raw": raw,
        "free_gacha": on or "gacha" in items or "free_gacha" in items,
        "start_level_50": on or "level" in items or "level50" in items,
        "all_dimensions": on or "maps" in items or "dimensions" in items,
        "all_shiny": on or "shiny" in items,
        "perfect_ivs": on or "iv" in items or "perfect_ivs" in items,
    }


# --------------------------------------------------------------------------
# API: overview
# --------------------------------------------------------------------------

@get("/admin/api/overview")
def api_overview(req):
    users = _count("users")
    online = 0
    if _has_col("users", "last_seen"):
        online = sum(1 for u in _all("SELECT last_seen FROM users LIMIT 5000")
                     if _is_online(u.get("last_seen")))
    rooms = _count("room_ids") if _table_exists("room_ids") else 0
    live_rooms = 0
    if _table_exists("room_ids") and _has_col("users", "room_id"):
        row = _one("SELECT COUNT(DISTINCT room_id) AS n FROM users WHERE room_id IS NOT NULL AND room_id != ''")
        live_rooms = int(row["n"]) if row else 0
    events = _count("event_queue") if _table_exists("event_queue") else 0
    mortys = _count("owned_morties") if _table_exists("owned_morties") else 0

    return {
        "server": {
            "version": "2.41.0",
            "uptime_seconds": int(time.time() - _STARTED_AT),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "host": config.PUBLIC_HOST,
            "port": config.PUBLIC_PORT,
            "bind": f"{config.BIND_HOST}:{config.BIND_PORT}",
            "base": config.public_base(),
        },
        "counts": {
            "users": users, "online": online, "rooms": rooms, "live_rooms": live_rooms,
            "events": events, "mortys": mortys,
        },
        "cheats": _cheats(),
        "announcement": _announcement(),
    }


def _announcement() -> str:
    try:
        from .. import telemetry
        return telemetry.get("announcement", "")
    except Exception:  # noqa: BLE001
        return ""


def _has_col(table: str, col: str) -> bool:
    try:
        rows = db.all_(f"PRAGMA table_info({table})")
        return any(r["name"] == col for r in rows)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# API: players
# --------------------------------------------------------------------------

@get("/admin/api/players")
def api_players(req):
    q = _q(req, "q").strip()
    limit = min(_qi(req, "limit", 100), 500)
    if q:
        rows = _all(
            """SELECT player_id, username, level, wins, losses, last_seen, room_id,
                      player_avatar_id, session_id
               FROM users
               WHERE username LIKE ? OR player_id LIKE ?
               ORDER BY last_seen DESC LIMIT ?""",
            (f"%{q}%", f"%{q}%", limit),
        )
    else:
        rows = _all(
            """SELECT player_id, username, level, wins, losses, last_seen, room_id,
                      player_avatar_id, session_id
               FROM users ORDER BY last_seen DESC LIMIT ?""",
            (limit,),
        )
    for r in rows:
        r["online"] = _is_online(r.get("last_seen"))
        r["morty_count"] = _count("owned_morties", "WHERE player_id = ?", (r["player_id"],)) \
            if _table_exists("owned_morties") else 0
    return {"players": rows, "total": _count("users")}


@get("/admin/api/player")
def api_player(req):
    pid = _q(req, "player_id")
    user = _one("SELECT * FROM users WHERE player_id = ?", (pid,))
    if not user:
        return {"error": "not found"}
    mortys = _all(
        """SELECT owned_morty_id, morty_id, level, hp, variant, is_locked
           FROM owned_morties WHERE player_id = ? ORDER BY id DESC LIMIT 60""",
        (pid,),
    )
    items = _all("SELECT item_id, amount FROM owned_items WHERE player_id = ? LIMIT 60", (pid,)) \
        if _table_exists("owned_items") else []
    decks = _all("SELECT deck_id, deck_name, owned_morty_ids FROM decks WHERE player_id = ?", (pid,)) \
        if _table_exists("decks") else []
    return {"user": user, "mortys": mortys, "items": items, "decks": decks}


# --------------------------------------------------------------------------
# API: rooms
# --------------------------------------------------------------------------

@get("/admin/api/rooms")
def api_rooms(req):
    rows = _all("SELECT * FROM room_ids LIMIT 200") if _table_exists("room_ids") else []
    for r in rows:
        rid = r.get("room_id")
        r["players"] = _all(
            "SELECT player_id, username, level, state FROM users WHERE room_id = ?", (rid,)
        ) if _has_col("users", "room_id") else []
        r["events"] = _count("event_queue", "WHERE room_id = ?", (rid,)) if _table_exists("event_queue") else 0
        r["pickups"] = _count(
            "event_queue",
            "WHERE room_id = ? AND event_name='room:pickup-added' AND pickup_id_collected_by_player_id IS NULL",
            (rid,),
        ) if _table_exists("event_queue") else 0
        r["wild_morties"] = _count(
            "event_queue", "WHERE room_id = ? AND event_name='room:wild-morty-added'", (rid,)
        ) if _table_exists("event_queue") else 0
        r["bots"] = _count(
            "event_queue", "WHERE room_id = ? AND event_name='room:bot-added'", (rid,)
        ) if _table_exists("event_queue") else 0
    return {"rooms": rows}


# --------------------------------------------------------------------------
# API: events
# --------------------------------------------------------------------------

@get("/admin/api/events")
def api_events(req):
    limit = min(_qi(req, "limit", 80), 500)
    room = _q(req, "room_id")
    if room:
        rows = _all(
            "SELECT * FROM event_queue WHERE room_id = ? ORDER BY id DESC LIMIT ?", (room, limit)
        ) if _table_exists("event_queue") else []
    else:
        rows = _all("SELECT * FROM event_queue ORDER BY id DESC LIMIT ?", (limit,)) \
            if _table_exists("event_queue") else []
    return {"events": rows}


# --------------------------------------------------------------------------
# API: config
# --------------------------------------------------------------------------

@get("/admin/api/config")
def api_config(req):
    return {
        "public_host": config.PUBLIC_HOST,
        "public_port": config.PUBLIC_PORT,
        "public_base": config.public_base(),
        "bind": f"{config.BIND_HOST}:{config.BIND_PORT}",
        "client_base": f"{config.public_base()}",
        "cheats": _cheats(),
        "worlds": getattr(config, "WORLDS", []),
        "ping_interval": getattr(config, "PING_INTERVAL_SECONDS", 30),
        "sse_keepalive": getattr(config, "SSE_KEEPALIVE_SECONDS", 30),
        "owned_morty_limit": getattr(config, "OWNED_MORTY_LIMIT", 750),
        "database": str(getattr(config, "RUNTIME_DB", "")),
        "env": {k: v for k, v in os.environ.items() if k.startswith("PMNET_")},
    }


# --------------------------------------------------------------------------
# API: console (read-only tail of the request log)
# --------------------------------------------------------------------------

# The launcher tees stdout into one of these; whichever exists and is freshest
# wins.  Missing files are not an error -- running under a terminal with no tee
# simply yields an empty console page.
_LOG_CANDIDATES = ("logs/server.log", "server.log", "/tmp/pmnet.log")


@get("/admin/api/console")
def api_console(req):
    limit = min(_qi(req, "limit", 120), 400)
    best: Path | None = None
    best_mtime = -1.0
    for rel in _LOG_CANDIDATES:
        p = Path(rel)
        if not p.is_absolute():
            p = Path(config.ROOT) / rel
        try:
            if p.is_file() and p.stat().st_mtime > best_mtime:
                best, best_mtime = p, p.stat().st_mtime
        except OSError:
            continue

    lines: list[str] = []
    if best is not None:
        try:
            lines = best.read_text(errors="replace").splitlines()[-limit:]
        except OSError:
            lines = []
    return {"lines": lines, "source": str(best) if best else None}


# --------------------------------------------------------------------------
# API: one-click world seeding
# --------------------------------------------------------------------------

@post("/admin/api/seed-room")
def api_seed_room(req):
    """Populate a room so a client can actually join it.

    ``/session/join-room`` refuses to place a player in a room that has no
    entities, so a fresh install needs at least one seeded room before the
    multiplayer lobby will open.  This drives :mod:`pmnet.rooms`, a direct port
    of the community server's ``room_entities.php``.

    Body: ``{"world_id": "1", "zone_id": "[13-15]", "spawn_all": false}``.
    With ``spawn_all`` every world is seeded.
    """
    from .. import rooms as rooms_mod
    body = req.json if isinstance(req.json, dict) else {}
    world = str(body.get("world_id") or "1")
    zone = str(body.get("zone_id") or "")

    try:
        if body.get("spawn_all"):
            out = []
            for w in ("1", "2", "3", "4", "5", "6", "7"):
                try:
                    out.append(rooms_mod.ensure_room_ready(w))
                except Exception as exc:  # noqa: BLE001
                    out.append({"world_id": w, "error": f"{type(exc).__name__}: {exc}"})
            return {"ok": True, "rooms": out}

        room = rooms_mod.ensure_room_ready(world, zone)
        if room is None:
            return {"ok": False,
                    "error": "room_ids 为空且无法创建，请检查数据库初始化"}
        return {"ok": True, "room": room, "entities": _entity_counts(room["room_id"])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _entity_counts(room_id: str) -> dict:
    from .. import rooms as rooms_mod
    try:
        snap = rooms_mod.build_room_snapshot(room_id)
        return {k: len(v) for k, v in snap.items()}
    except Exception:  # noqa: BLE001
        return {}


@post("/admin/api/cheat")
def api_cheat(req):
    """Report the cheat switches.  They are read from the environment at boot,
    so this endpoint describes rather than mutates."""
    return {"ok": True, "cheats": _cheats(),
            "note": "作弊开关在启动时读取环境变量 PMNET_CHEAT，改完重启生效。"}


# --------------------------------------------------------------------------
# API: connected devices
# --------------------------------------------------------------------------

@get("/admin/api/devices")
def api_devices(req):
    """Every client that has talked to this server, newest first.

    The ``kind`` column is what makes this useful on an emulator: the game
    (``UnityPlayer/...``), the OneTrust WebView, the panel's own browser tab and
    anonymous probes all share 127.0.0.1, and only the User-Agent separates
    them.
    """
    from .. import telemetry
    return {
        "devices": telemetry.list_clients(limit=min(_qi(req, "limit", 100), 300)),
        "summary": telemetry.summary(),
    }


@post("/admin/api/devices/forget")
def api_device_forget(req):
    from .. import telemetry
    body = req.json if isinstance(req.json, dict) else {}
    cid = body.get("id")
    if cid is None:
        return {"ok": False, "error": "缺少 id"}
    return {"ok": telemetry.forget(int(cid))}


# --------------------------------------------------------------------------
# API: live settings
# --------------------------------------------------------------------------

@get("/admin/api/settings")
def api_settings(req):
    from .. import telemetry
    return {"schema": telemetry.schema(), "values": telemetry.get_settings()}


@post("/admin/api/settings")
def api_settings_save(req):
    from .. import telemetry
    body = req.json if isinstance(req.json, dict) else {}
    values = body.get("values") if isinstance(body.get("values"), dict) else body
    saved = telemetry.set_settings({k: v for k, v in values.items()
                                    if not k.startswith("_")})
    return {"ok": True, "values": saved}


# --------------------------------------------------------------------------
# API: player actions (grant / reset / kick)
# --------------------------------------------------------------------------

@post("/admin/api/player/action")
def api_player_action(req):
    """Operator actions on one account.

    body: ``{"player_id": ..., "action": "grant"|"kick"|"reset_saves"|"unlock",
             "coins": 0, "coupons": 0}``
    """
    body = req.json if isinstance(req.json, dict) else {}
    pid = str(body.get("player_id") or "")
    action = str(body.get("action") or "")
    if not pid or not action:
        return {"ok": False, "error": "缺少 player_id 或 action"}

    user = _one("SELECT * FROM users WHERE player_id = ?", (pid,))
    if not user:
        return {"ok": False, "error": "账号不存在"}

    try:
        if action == "grant":
            coins = int(body.get("coins") or 0)
            coupons = int(body.get("coupons") or 0)
            permits = int(body.get("permits") or 0)
            db.run(
                """UPDATE users
                      SET coins = COALESCE(coins,0) + ?,
                          coupons = COALESCE(coupons,0) + ?,
                          permits = COALESCE(permits,0) + ?
                    WHERE player_id = ?""",
                (coins, coupons, permits, pid))
            return {"ok": True, "granted": {"coins": coins, "coupons": coupons,
                                            "permits": permits}}

        if action == "kick":
            # Clearing session_id invalidates the JWT the client holds, so its
            # next call gets 401 and it drops back to the title screen.
            db.run("UPDATE users SET session_id = NULL WHERE player_id = ?", (pid,))
            return {"ok": True, "kicked": True}

        if action == "unlock":
            db.run("""UPDATE users SET level = MAX(COALESCE(level,1), 50)
                       WHERE player_id = ?""", (pid,))
            return {"ok": True, "level": 50}

        if action == "heal":
            db.run("UPDATE owned_morties SET hp = 9999 WHERE player_id = ?", (pid,)) \
                if _table_exists("owned_morties") else None
            return {"ok": True, "healed": True}

        return {"ok": False, "error": f"未知操作 {action}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# API: bulk room seeding with custom counts
# --------------------------------------------------------------------------

@post("/admin/api/rooms/seed")
def api_rooms_seed(req):
    """Seed rooms with operator-chosen entity counts.

    body: ``{"worlds": ["1","2"], "pickups": 3, "wilds": 5, "bots": 4,
             "force": false}``
    """
    from .. import rooms as rooms_mod, telemetry

    body = req.json if isinstance(req.json, dict) else {}
    worlds = body.get("worlds")
    if not isinstance(worlds, list) or not worlds:
        worlds = ["1", "2", "3", "4", "5", "6", "7"]
    worlds = [str(w) for w in worlds][:20]

    if "pickups" in body or "wilds" in body or "bots" in body:
        save = {}
        if "pickups" in body: save["room_pickups"] = str(body["pickups"])
        if "wilds" in body: save["room_wilds"] = str(body["wilds"])
        if "bots" in body: save["room_bots"] = str(body["bots"])
        telemetry.set_settings(save)

    out = []
    for w in worlds:
        try:
            room = rooms_mod.ensure_room_ready(w)
            if room is None:
                out.append({"world_id": w, "error": "没有可用的房间行"})
                continue
            snapshot = rooms_mod.build_room_snapshot(room["room_id"])
            out.append({
                "world_id": w,
                "room_id": room["room_id"],
                "counts": {k: len(v) for k, v in snapshot.items()},
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"world_id": w, "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": True, "rooms": out}


# --------------------------------------------------------------------------
# API: room cleanup
# --------------------------------------------------------------------------

@post("/admin/api/rooms/clear")
def api_rooms_clear(req):
    """Drop a room's queued events (entities and players both reset)."""
    body = req.json if isinstance(req.json, dict) else {}
    room_id = str(body.get("room_id") or "")
    if not room_id:
        return {"ok": False, "error": "缺少 room_id"}
    try:
        db.run("DELETE FROM event_queue WHERE room_id = ?", (room_id,))
        db.run("UPDATE users SET room_id = NULL WHERE room_id = ?", (room_id,))
        return {"ok": True, "cleared": room_id}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# API: database / system maintenance
# --------------------------------------------------------------------------

@get("/admin/api/system")
def api_system(req):
    """Row counts per table plus on-disk sizes -- a quick health read."""
    tables: list[dict] = []
    try:
        names = [r["name"] for r in db.all_(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    except Exception:  # noqa: BLE001
        names = []
    for name in names:
        tables.append({"table": name, "rows": _count(name)})

    files = []
    for rel in ("data/pmnet.db", "data/pmnet.db-wal", "logs/server.log"):
        p = Path(config.ROOT) / rel
        try:
            if p.is_file():
                files.append({"path": rel, "bytes": p.stat().st_size})
        except OSError:
            pass

    return {"tables": tables, "files": files,
            "settings_schema": __import__("pmnet.telemetry", fromlist=["x"]).schema()}


@post("/admin/api/announcement")
def api_announcement(req):
    from .. import telemetry
    body = req.json if isinstance(req.json, dict) else {}
    text = str(body.get("text") or "")[:500]
    telemetry.set_settings({"announcement": text})
    return {"ok": True, "announcement": text}
