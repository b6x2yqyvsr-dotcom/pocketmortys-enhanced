"""Asset / metadata CDN endpoints.

The original client resolves three logical hosts, all of which our metadata
patch has collapsed onto this one server:

* ``newc137.bps-pmnet.com``  -- the JSON API
* ``assets.bps-pmnet.com``   -- AssetBundles + the "Aliases" manifest
* ``game.bps-pmnet.com``     -- world configuration

This module covers everything that is *not* a JSON API call: the clock sync,
the status probe, the AssetBundle alias manifest, the bundles themselves and
the world config.

Serving files
-------------
Anything under ``cdn/`` is served verbatim.  The alias manifest is special:
it stores **absolute** download URLs pointing at whatever host baked it in, so
it is rewritten on the fly to point back at this server.  Without that rewrite
the client would happily fetch its bundles from the original community host
while talking to us for everything else -- which works, right up until that
host disappears too.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import assets, config
from ..http import Response, get, json_response

CDN_ROOT = config.ROOT / "cdn"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _safe_join(root: Path, *parts: str) -> Path | None:
    """Join path parts under root, refusing anything that escapes it."""
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _file_response(path: Path, content_type: str = "application/octet-stream",
                   download_name: str | None = None) -> Response:
    headers = {}
    if download_name:
        headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return Response(status=200, body=path.read_bytes(),
                    content_type=content_type, headers=headers)


# --------------------------------------------------------------------------
# clock sync
# --------------------------------------------------------------------------

@get("/time")
def server_time(req):
    """Used by ServerTimeController to compute a client/server clock offset.

    ``utc_timestamp`` is the field the reference private server returns, and it
    is MILLISECONDS rather than seconds.  The extra spellings below are kept
    because the metadata lists ``serverTime``/``utc_time`` as neighbouring
    identifiers; parsers ignore unknown keys, so emitting all of them is
    cheaper than guessing which one the client reads.
    """
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
    return {
        "utc_timestamp": int(now * 1000),
        "serverTime": int(now),
        "server_time": int(now),
        "utc_time": iso,
        "utcTime": iso,
        "time": int(now),
        "timestamp": int(now),
    }


# --------------------------------------------------------------------------
# 联网检测探针
# --------------------------------------------------------------------------

@get("/generate_204")
@get("/gen_204")
def generate_204(req):
    """Android / Unity 的联网检测端点，约定**必须**返回 204。

    客户端在启动时会打 ``clients3.google.com/generate_204``
    （metadata 里已被改写到本机）来判断「有没有网」。
    这个端点只要不是 204 就会被判成「网络不可用」，游戏于是完全走本地缓存、
    一个 API 请求都不发 —— 表现就是服务器端「看不到设备」。

    community 那版 PHP 的 .htaccess 白名单里没有它，返回 403，
    正是这条把不少人卡住的。这里显式实现，并且**不返回任何 body**
    （204 带 body 是协议违规）。
    """
    return Response(status=204)


# --------------------------------------------------------------------------
# status probe
# --------------------------------------------------------------------------

@get("/Status")
def status_root(req):
    """Reference implementation ships an empty Status/rat.json -- so does ours."""
    return {}


@get("/Status/rat.json")
def status_rat(req):
    return {}


# --------------------------------------------------------------------------
# world configuration
# --------------------------------------------------------------------------

@get("/pocketmortynet/config/worlds.json")
def worlds_config(req):
    """``kWorldsJSONPocketMortyNetPath`` -- the per-world level windows.

    We serve the same table the session:start payload advertises so the two
    can never disagree.
    """
    return {"worlds": config.WORLDS}


# --------------------------------------------------------------------------
# alias manifest + bundles
# --------------------------------------------------------------------------

@get("/Aliases/<group>/<platform>")
def aliases(req, group: str, platform: str):
    path = _safe_join(CDN_ROOT, "Aliases", group, platform, "manifest.json")
    if not path or not path.is_file():
        return json_response({"error": "NOT_FOUND"}, status=404)

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json_response({"error": "BAD_MANIFEST"}, status=500)

    # The client keeps a copy of this manifest in AssetBundle.dat and compares
    # it against what the server returns.  Rewriting the hosts makes the two
    # differ, which the client reports as "outdated assets" and refuses to
    # proceed on -- so by default the manifest is served verbatim.
    #
    # Set PMNET_MANIFEST_REWRITE=1 to re-anchor the bundle URLs on our own
    # origin instead (needed only if the client turns out not to rewrite the
    # host itself).
    if os.environ.get("PMNET_MANIFEST_REWRITE", "") == "1":
        base = config.public_base()
        for entry in manifest.values():
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str):
                continue
            _, _, after_scheme = url.partition("://")
            _, slash, rest = after_scheme.partition("/")
            if slash:
                entry["url"] = f"{base}/{rest}"

    return manifest


@get("/AssetBundles/<path:rest>")
def asset_bundle(req, rest: str):
    """Serve an AssetBundle, extracting it on demand if necessary.

    The bundle comes from the local cache, or -- on a miss -- straight out of
    one of the player's own game archives (see pmnet.assets).  Nothing has to
    be placed here by hand, and no third-party host is involved.
    """
    parts = [p for p in rest.split("/") if p not in ("", ".", "..")]
    relative = "/".join(parts)

    data = assets.fetch(relative)
    if data is None:
        resolvable, advertised = assets.available()
        return json_response({
            "error": "BUNDLE_NOT_AVAILABLE",
            "path": relative,
            "detail": ("No cache entry and no configured source contains this "
                       "bundle. Point the server at your own game files with "
                       "the PMNET_ASSET_SOURCES environment variable, or drop "
                       "the data package into data/sources/."),
            "bundles_resolvable": resolvable,
            "bundles_advertised": advertised,
        }, status=404)

    return Response(status=200, body=data,
                    content_type="application/octet-stream",
                    headers={"Content-Disposition":
                             f'attachment; filename="{Path(rest).name}"',
                             "X-Asset-Cache": "hit"})
