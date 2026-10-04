"""Client telemetry: who is talking to this server, and what they asked for.

Every request funnels through :func:`record`, which keeps a per-client row in
the ``clients`` table plus a bounded activity log.  The control panel's
"设备" page reads it back.

A *client* is identified by ``(remote address, User-Agent)``.  That matters
here because several very different callers share ``127.0.0.1`` on an emulator
or with ``adb reverse`` in play:

* the game itself -- ``UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, ...)``
* the OneTrust consent WebView -- a Chrome UA
* the control panel in a desktop browser
* bare ``GET /`` probes with no UA at all

Grouping by address alone would merge all four into one useless row.

Everything is best-effort: if the database is missing a column or the write
fails, requests keep working and the panel simply shows less.
"""

from __future__ import annotations

import json
import time

from . import db

# Requests kept per client in the rolling activity log.
ACTIVITY_LIMIT = 40

# A client with no traffic for this long is shown as offline.
ONLINE_WINDOW_SECONDS = 120


def _ensure_tables() -> None:
    try:
        db.connect().executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint  TEXT UNIQUE NOT NULL,
                address      TEXT,
                user_agent   TEXT,
                kind         TEXT,
                player_id    TEXT,
                first_seen   INTEGER NOT NULL,
                last_seen    INTEGER NOT NULL,
                hits         INTEGER NOT NULL DEFAULT 0,
                errors       INTEGER NOT NULL DEFAULT 0,
                last_path    TEXT,
                activity     TEXT
            );
            CREATE TABLE IF NOT EXISTS server_settings (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL,
                updated INTEGER NOT NULL
            );
            """
        )
    except Exception:  # noqa: BLE001
        pass


def classify(user_agent: str) -> str:
    """Human label for a User-Agent string."""
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    if "unityplayer" in ua or "unitywebrequest" in ua:
        return "game"
    if "curl" in ua:
        return "curl"
    if any(k in ua for k in ("okhttp", "dalvik", "android")):
        return "android"
    if any(k in ua for k in ("mozilla", "chrome", "safari", "firefox", "edg/")):
        return "browser"
    if "python" in ua:
        return "script"
    return "other"


def record(address: str, method: str, path: str, status: int,
           user_agent: str = "", player_id: str | None = None) -> None:
    """Note one request.  Never raises."""
    try:
        _ensure_tables()
        ua = (user_agent or "").strip()
        fingerprint = f"{address}|{ua[:120]}"
        now = int(time.time())
        entry = {"t": now, "m": method, "p": path[:180], "s": status}

        row = db.one("SELECT id, hits, errors, activity FROM clients WHERE fingerprint = ?",
                     (fingerprint,))
        if row is None:
            db.run(
                """INSERT INTO clients
                       (fingerprint, address, user_agent, kind, player_id,
                        first_seen, last_seen, hits, errors, last_path, activity)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (fingerprint, address, ua, classify(ua), player_id,
                 now, now, 1, 1 if status >= 400 else 0, path[:180],
                 json.dumps([entry], separators=(",", ":"))),
            )
            return

        try:
            activity = json.loads(row["activity"] or "[]")
            if not isinstance(activity, list):
                activity = []
        except (TypeError, ValueError):
            activity = []
        activity.append(entry)
        activity = activity[-ACTIVITY_LIMIT:]

        db.run(
            """UPDATE clients
                  SET last_seen = ?, hits = hits + 1,
                      errors = errors + ?, last_path = ?, activity = ?,
                      player_id = COALESCE(?, player_id)
                WHERE id = ?""",
            (now, 1 if status >= 400 else 0, path[:180],
             json.dumps(activity, separators=(",", ":")), player_id, row["id"]),
        )
    except Exception:  # noqa: BLE001
        pass


def list_clients(limit: int = 100) -> list[dict]:
    """Every client seen, newest first, with its activity decoded."""
    try:
        _ensure_tables()
        rows = db.rowdicts(db.all_(
            "SELECT * FROM clients ORDER BY last_seen DESC LIMIT ?", (limit,)))
    except Exception:  # noqa: BLE001
        return []

    now = int(time.time())
    for r in rows:
        r["online"] = (now - int(r.get("last_seen") or 0)) < ONLINE_WINDOW_SECONDS
        r["idle_seconds"] = now - int(r.get("last_seen") or now)
        try:
            act = json.loads(r.get("activity") or "[]")
            r["activity"] = act if isinstance(act, list) else []
        except (TypeError, ValueError):
            r["activity"] = []
        # Short label for the table's address column.
        r["label"] = r.get("user_agent") or "(无 User-Agent)"
    return rows


def summary() -> dict:
    """Counts by client kind, for the overview cards."""
    try:
        _ensure_tables()
        rows = db.rowdicts(db.all_("SELECT kind, last_seen FROM clients"))
    except Exception:  # noqa: BLE001
        return {"total": 0, "online": 0, "by_kind": {}}

    now = int(time.time())
    by_kind: dict[str, int] = {}
    online = 0
    for r in rows:
        kind = str(r.get("kind") or "other")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if (now - int(r.get("last_seen") or 0)) < ONLINE_WINDOW_SECONDS:
            online += 1
    return {"total": len(rows), "online": online, "by_kind": by_kind}


def forget(client_id: int) -> bool:
    try:
        _ensure_tables()
        db.run("DELETE FROM clients WHERE id = ?", (int(client_id),))
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# live settings
# --------------------------------------------------------------------------

# Everything an operator can change without restarting.  ``kind`` drives the
# panel's widget, ``default`` seeds the row on first read.
SETTING_DEFS: list[dict] = [
    {"key": "announcement", "kind": "text", "default": "",
     "label": "服务器公告", "help": "显示在面板顶部，留空则不显示"},
    {"key": "auto_seed_rooms", "kind": "bool", "default": "1",
     "label": "自动生成房间内容", "help": "启动时给 7 个世界填充实体，多人模式需要"},
    {"key": "room_pickups", "kind": "int", "default": "3",
     "label": "每房间拾取物", "help": "新房间生成的拾取物数量"},
    {"key": "room_wilds", "kind": "int", "default": "5",
     "label": "每房间野生莫蒂", "help": "新房间生成的野生莫蒂数量"},
    {"key": "room_bots", "kind": "int", "default": "4",
     "label": "每房间机器人", "help": "新房间生成的机器人数量"},
    {"key": "welcome_coins", "kind": "int", "default": "5000",
     "label": "新号金币", "help": "注册时发放的金币"},
    {"key": "welcome_coupons", "kind": "int", "default": "50",
     "label": "新号奖券", "help": "注册时发放的奖券"},
    {"key": "start_level", "kind": "int", "default": "50",
     "label": "新号等级", "help": "注册时的初始等级"},
    {"key": "all_shiny", "kind": "bool", "default": "1",
     "label": "全部闪亮", "help": "所有莫蒂都是闪亮变体"},
    {"key": "perfect_ivs", "kind": "bool", "default": "1",
     "label": "完美 IV", "help": "所有莫蒂 16/16/16/16"},
    {"key": "free_gacha", "kind": "bool", "default": "1",
     "label": "免费扭蛋", "help": "扭蛋不消耗奖券"},
    {"key": "all_dimensions", "kind": "bool", "default": "1",
     "label": "全地图解锁", "help": "新号解锁所有次元"},
    {"key": "manifest_rewrite", "kind": "bool", "default": "0",
     "label": "改写清单地址", "help": "把 manifest 里的资源包 URL 改指向本机"},
    {"key": "log_requests", "kind": "bool", "default": "1",
     "label": "记录请求日志", "help": "关闭可提升性能，但面板看不到请求"},
]


def _defaults() -> dict[str, str]:
    return {d["key"]: d["default"] for d in SETTING_DEFS}


def get_settings() -> dict[str, str]:
    out = _defaults()
    try:
        _ensure_tables()
        for r in db.rowdicts(db.all_("SELECT key, value FROM server_settings")):
            out[str(r["key"])] = str(r["value"])
    except Exception:  # noqa: BLE001
        pass
    return out


def get(key: str, fallback: str = "") -> str:
    return get_settings().get(key, fallback)


def get_bool(key: str, fallback: bool = False) -> bool:
    v = get(key, "1" if fallback else "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(get(key, str(fallback)))
    except (TypeError, ValueError):
        return fallback


def set_settings(values: dict) -> dict[str, str]:
    """Persist a partial update, ignoring unknown keys."""
    known = {d["key"] for d in SETTING_DEFS}
    try:
        _ensure_tables()
        now = int(time.time())
        for k, v in values.items():
            if k not in known:
                continue
            db.run(
                """INSERT INTO server_settings (key, value, updated) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                  updated=excluded.updated""",
                (str(k), str(v), now),
            )
    except Exception:  # noqa: BLE001
        pass
    return get_settings()


def schema() -> list[dict]:
    """Setting definitions with current values, for the panel."""
    current = get_settings()
    return [{**d, "value": current.get(d["key"], d["default"])}
            for d in SETTING_DEFS]
