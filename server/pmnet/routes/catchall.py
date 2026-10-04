"""Catch-all probe.

Every request the client makes is already logged, but a request that matches
no route never reaches a handler, so we cannot tell *which* path the client
asked for from the log alone.  This module answers the plausible
server-message paths (``manifest.json`` and friends) and records anything
else, which is how the "Unable to find manifest resource." popup gets pinned
to an actual URL.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..http import Response, get

_ROOT = Path(__file__).resolve().parent.parent.parent / "cdn"
_MANIFEST = _ROOT / "Aliases" / "rat" / "Android" / "manifest.json"
_SEEN = _ROOT.parent / "recordings" / "catchall.log"


def _manifest() -> Response:
    if not _MANIFEST.is_file():
        return Response(status=404, body=b"{}", content_type="application/json")
    return Response(status=200, body=_MANIFEST.read_bytes(),
                    content_type="application/json; charset=utf-8")


def _note(path: str) -> None:
    try:
        _SEEN.parent.mkdir(parents=True, exist_ok=True)
        with _SEEN.open("a", encoding="utf-8") as fh:
            fh.write(path + "\n")
    except OSError:
        pass


@get("/manifest.json")
def manifest_root(req):
    _note("/manifest.json")
    return _manifest()


@get("/Status/manifest.json")
def manifest_status(req):
    _note("/Status/manifest.json")
    return _manifest()


@get("/Aliases/manifest.json")
def manifest_aliases(req):
    _note("/Aliases/manifest.json")
    return _manifest()


@get("/<path:rest>/manifest.json")
def manifest_any(req, rest: str):
    _note(f"/{rest}/manifest.json")
    return _manifest()
