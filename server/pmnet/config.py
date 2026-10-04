"""Runtime configuration for the Pocket Mortys private server.

Everything the client needs to be told about "where am I" lives here, because
the original client hardcodes three separate hosts and we replace all of them
with a single LAN address.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RECORD_DIR = ROOT / "recordings"

LEGACY_DB = DATA_DIR / "legacy.db"      # imported from the PHP server dump
RUNTIME_DB = DATA_DIR / "pmnet.db"      # live server database

# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------

BIND_HOST = os.environ.get("PMNET_BIND", "0.0.0.0")
BIND_PORT = int(os.environ.get("PMNET_PORT", "8080"))

VERBOSE = os.environ.get("PMNET_VERBOSE", "1") not in ("0", "", "false")


def detect_lan_ip() -> str:
    """Best-effort discovery of the LAN address this machine is reachable at.

    Set ``PMNET_PUBLIC_HOST`` to override.  That override is the intended
    escape hatch when this guess is wrong -- which it is whenever a VPN owns
    the default route, because the routing trick below then reports the tunnel
    address rather than the Wi-Fi one the phone needs.
    """
    override = os.environ.get("PMNET_PUBLIC_HOST")
    if override:
        return override
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is actually sent; this just picks the outbound interface.
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


PUBLIC_HOST = detect_lan_ip()
PUBLIC_PORT = int(os.environ.get("PMNET_PUBLIC_PORT", str(BIND_PORT)))

# The scheme the patched client will be pointed at.  We deliberately use plain
# HTTP: the client's networking is pure C# sockets (Best HTTP), so Android's
# cleartext policy never sees it, and it removes the entire TLS + certificate
# pinning problem that HTTPS would create for a self-hosted server.
PUBLIC_SCHEME = os.environ.get("PMNET_SCHEME", "http")


def public_base() -> str:
    """e.g. ``http://192.168.1.42:8080`` -- no trailing slash."""
    host = PUBLIC_HOST
    if (PUBLIC_SCHEME == "http" and PUBLIC_PORT == 80) or (
        PUBLIC_SCHEME == "https" and PUBLIC_PORT == 443
    ):
        return f"{PUBLIC_SCHEME}://{host}"
    return f"{PUBLIC_SCHEME}://{host}:{PUBLIC_PORT}"


# Original hosts baked into the client.  The metadata patcher rewrites these.
LEGACY_HOSTS = {
    "api": "newc137.bps-pmnet.com",
    "assets": "assets.bps-pmnet.com",
    "game": "game.bps-pmnet.com",
}

# --------------------------------------------------------------------------
# Protocol constants recovered from the client / PHP reference
# --------------------------------------------------------------------------

SESSION_URL_TTL = 60          # seconds, as advertised to the client
SSE_KEEPALIVE_SECONDS = 30
PING_INTERVAL_SECONDS = 30

OWNED_MORTY_LIMIT = 750

# worlds advertised in the ``session:start`` payload
WORLDS = [
    {"world_id": "1", "player_level": {"min": 1, "max": 50}},
    {"world_id": "2", "player_level": {"min": 5, "max": 50}},
    {"world_id": "3", "player_level": {"min": 15, "max": 50}},
    {"world_id": "4", "player_level": {"min": 30, "max": 50}},
    {"world_id": "5", "player_level": {"min": 5, "max": 50}},
    {"world_id": "6", "player_level": {"min": 10, "max": 50}},
    {"world_id": "7", "player_level": {"min": 15, "max": 50}},
]

# XP curve seeded by the reference implementation for a fresh player.
STARTER = {
    "level": 1,
    "xp": 27,
    "xp_lower": 27,
    "xp_upper": 64,
    "streak": 0,
    "coins": 0,
    "coupons": 0,
    "permits": 0,
    "decks_owned": 3,
    "active_deck_id": 0,
}
