"""SQLite access layer.

The server keeps two databases:

* ``data/legacy.db`` -- read-only seed data lifted from the original game
  servers via the community PHP server's MySQL dump (gacha pools, drop rates,
  room routing, raid events, deck config).
* ``data/pmnet.db``  -- the live, mutable world: users, morties, decks,
  rooms, event queue.

At first run the live DB is created by copying every *non-player* table out of
the legacy DB, so the game config ships with the server instead of being
re-imported by hand.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

_local = threading.local()

# Tables that describe the world rather than a player; copied from legacy.db.
CONFIG_TABLES = [
    "deck_config",
    "gachas",
    "gacha_contents",
    "gacha_content_items",
    "gacha_drop_rates",
    "gacha_promos",
    "gacha_promo_mortys",
    "gacha_promo_morty_attacks",
    "gacha_promo_attack_effects",
    "room_ids",
    "events",
]

# Tables the live server owns outright.
LIVE_TABLES = [
    "users",
    "decks",
    "owned_morties",
    "owned_attacks",
    "owned_items",
    "owned_avatars",
    "mortydex",
    "friend_list",
    "event_queue",
    "room_ids",
    "user_sessions",
    # server-side additions
    "battles",
    "rooms",
    "sessions",
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Return a thread-local connection with sane pragmas."""
    path = path or config.RUNTIME_DB
    key = str(path)
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(key)
    if conn is None:
        conn = sqlite3.connect(key, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA busy_timeout=15000")
        conns[key] = conn
    return conn


def legacy() -> sqlite3.Connection:
    return connect(config.LEGACY_DB)


# --------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------

def _auto_import_legacy() -> bool:
    """Build ``data/legacy.db`` from the bundled PHP dump when it is missing.

    The shipped archive normally carries ``data/legacy.db`` directly, but a user
    who copies only the source tree still gets a working server: the community
    schema snapshot lives at ``reference/community-php/pocket_mortys.sql`` and
    ``tools/import_legacy_dump.py`` converts it.  Returns True on success.
    """
    sql = config.ROOT / "reference" / "community-php" / "pocket_mortys.sql"
    tool = config.ROOT / "tools" / "import_legacy_dump.py"
    if not sql.is_file() or not tool.is_file():
        return False
    import subprocess
    import sys as _sys
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [_sys.executable, str(tool), str(sql), str(config.LEGACY_DB)],
            cwd=str(config.ROOT), capture_output=True, text=True, timeout=300,
        )
        return proc.returncode == 0 and config.LEGACY_DB.exists()
    except Exception:  # noqa: BLE001
        return False


def bootstrap(force: bool = False) -> None:
    """Create the live DB from the legacy one if it does not exist yet."""
    if config.RUNTIME_DB.exists() and not force:
        _ensure_extra_tables()
        return

    if not config.LEGACY_DB.exists():
        _auto_import_legacy()

    if not config.LEGACY_DB.exists():
        # Last resort: an empty-but-valid database.  Players can register and
        # the panel works; only the world config tables stay empty until the
        # operator supplies legacy.db.
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _ensure_extra_tables()
        return

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if config.RUNTIME_DB.exists():
        config.RUNTIME_DB.unlink()

    con = connect()
    src = legacy()

    # 1. schema for config tables + player tables, copied structurally
    for table in CONFIG_TABLES + [
        "users", "decks", "owned_morties", "owned_attacks",
        "owned_items", "owned_avatars", "mortydex", "friend_list",
        "event_queue", "user_sessions",
    ]:
        ddl = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if ddl and ddl[0]:
            con.execute(ddl[0])

    # 2. copy the world config verbatim
    for table in CONFIG_TABLES:
        cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
        rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
        if not rows:
            continue
        placeholders = ",".join("?" * len(cols))
        collist = ",".join(f'"{c}"' for c in cols)
        con.executemany(
            f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})',
            [tuple(r) for r in rows],
        )

    _ensure_extra_tables()
    con.commit()


def _ensure_extra_tables() -> None:
    """Tables the Python server adds on top of the PHP schema.

    Every statement here is ``IF NOT EXISTS`` and the indexes are created
    *after* a defensive re-check, because this runs on two very different
    databases:

    * a bootstrapped one, where the PHP-derived tables are already present;
    * a brand-new ``pmnet.db`` that ``connect()`` just created empty, where
      they are not.

    Without the guard a fresh install died with
    ``no such table: main.event_queue`` the moment the first index was built.
    """
    con = connect()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT UNIQUE NOT NULL,
            player_id     TEXT NOT NULL,
            jwt           TEXT,
            created_at    INTEGER NOT NULL,
            expires_at    INTEGER NOT NULL,
            sse_open      INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS rooms (
            room_id       TEXT PRIMARY KEY,
            world_id      TEXT,
            zone_id       TEXT,
            udp_host      TEXT,
            udp_port      TEXT,
            created_at    INTEGER NOT NULL
        );

        -- One authoritative battle.  The client is a thin renderer: every
        -- turn is computed here and pushed back over SSE.
        CREATE TABLE IF NOT EXISTS battles (
            battle_id       TEXT PRIMARY KEY,
            room_id         TEXT,
            player_id       TEXT NOT NULL,
            kind            TEXT NOT NULL,      -- wild|bot|boss|pvp|raid
            state           TEXT NOT NULL,      -- json blob
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            finished        INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # Indexes need their tables.  On a bare database the PHP-derived ones are
    # absent, so skip rather than abort the whole bootstrap.
    wanted = [
        ("event_queue", "idx_event_queue_room", "room_id, id"),
        ("owned_morties", "idx_owned_morties_player", "player_id"),
        ("users", "idx_users_session", "session_id"),
        ("users", "idx_users_secret", "secret"),
        ("users", "idx_users_room", "room_id"),
    ]
    present = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, index, cols in wanted:
        if table not in present:
            continue
        try:
            con.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table}({cols})")
        except sqlite3.OperationalError:
            # A column may be missing on an older image; not fatal.
            pass


# --------------------------------------------------------------------------
# tiny query helpers
# --------------------------------------------------------------------------

def one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def all_(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def run(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    return connect().execute(sql, params)


def now() -> int:
    return int(time.time())


def iso_now() -> str:
    """Timestamp shape the client expects (matches the PHP reference)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def rowdict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rowdicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
