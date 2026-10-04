"""Route registration.

Importing this package wires every endpoint into the router in
:mod:`pmnet.http`.  Modules are imported for their side effect (the
``@get``/``@post`` decorators), so a module that is never imported is a module
whose endpoints silently do not exist.
"""

from __future__ import annotations

# Order is irrelevant; each module self-registers.
from . import user          # noqa: F401  /user/register /user/login /session/ping-dynamic
from . import session       # noqa: F401  /session/state /session/player-details /session/join-room ...
from . import gacha         # noqa: F401  /session/gacha /session/gacha-info /sp/gacha-info /iap/display/list
from . import admin         # noqa: F401  /admin + /admin/api/*
from . import cdn           # noqa: F401  /time /Status /Aliases/<g>/<p> /AssetBundles/** /pocketmortynet/config/worlds.json
from . import sse_route     # noqa: F401  /sse

# Optional endpoint groups.  Imported defensively so that a module which
# fails -- or which re-registers a path another module already owns, which
# raises RuntimeError -- cannot stop the server from booting.
for _optional in ("battle", "social", "misc", "consent", "onetrust", "catchall", "mp_missing"):
    try:
        __import__(f"{__name__}.{_optional}")
    except (ImportError, RuntimeError) as _exc:  # pragma: no cover
        print(f"[routes] warning: optional module '{_optional}' unavailable: {_exc}")

__all__ = ["user", "session", "gacha", "cdn", "sse_route", "admin"]
