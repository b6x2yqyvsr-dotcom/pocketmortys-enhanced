"""OneTrust banner-SDK endpoints.

The client's privacy-consent flow is driven by the OneTrust SDK, which fetches
its banner configuration from ``mobile-data.onetrust.io``.  Our metadata patch
re-anchors that host on this server, so the same response has to be served
here -- otherwise the SDK never gets a config, the consent banner never
resolves, and the game sits on "must be online to view the privacy policy".

The payload under ``cdn/bannersdk/`` is the response captured from a working
install, replayed verbatim.
"""

from __future__ import annotations

from pathlib import Path

from ..http import Response, get

_ROOT = Path(__file__).resolve().parent.parent.parent / "cdn" / "bannersdk"


def _json_file(rel: str) -> Response:
    path = _ROOT / rel
    if not path.is_file():
        return Response(status=404,
                        body=b'{"status":{"application":{"code":404,"msg":"not found"}}}',
                        content_type="application/json; charset=utf-8")
    return Response(status=200, body=path.read_bytes(),
                    content_type="application/json; charset=utf-8")


@get("/bannersdk/v2/applicationdata/")
def application_data(req):
    return _json_file("v2/applicationdata")
