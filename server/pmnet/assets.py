"""Self-contained asset store.

Design goal
-----------
The server must be able to serve the game's AssetBundles **without anyone
pre-placing files**, and without depending on any third-party host.  The only
inputs are the player's own game files:

* the game data package (``files/UnityCache/Shared/<bundle>/<hash>/__data``),
  which is what the client itself downloaded and cached, and
* optionally a directory of already-extracted ``.assetbundle`` files.

Resolution order for ``AssetBundles/<group>/<platform>/<name>.assetbundle``:

1. the local cache (``cdn/AssetBundles/...``) -- fastest, and what gets served
2. any configured *source*, from which the bundle is extracted on demand and
   then written into the cache

Sources are declared with ``PMNET_ASSET_SOURCES`` (an ``os.pathsep``-separated
list of directories or zip files).  A zip is read in place, so pointing at the
original 145 MB data package costs no disk and no unpacking step.

The same machinery backs :func:`ensure_gamedata`, which pulls the JSON data
tables (``ItemInfo``, ``AttackInfo``, ``MortyInfo`` ...) out of the bundles the
first time they are needed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import zipfile
from pathlib import Path

from . import config

# `<bundle>/<hash>/__data` inside a data package
CACHE_ENTRY_RE = re.compile(
    r"(?:^|/)UnityCache/Shared/(?P<bundle>[^/]+)/[0-9a-f]+/__data$"
)

_lock = threading.Lock()
_manifest_cache: dict[str, str] | None = None


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def sources() -> list[Path]:
    """Configured origins, in priority order.

    ``PMNET_ASSET_SOURCES`` may list directories or zip files.  A bare path to
    the original data package (``口蘑数据包(1).zip``) is the common case.
    """
    raw = os.environ.get("PMNET_ASSET_SOURCES", "")
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            out.append(Path(part).expanduser())
    # also auto-discover anything dropped into data/sources/
    drop = config.ROOT / "data" / "sources"
    if drop.is_dir():
        for entry in sorted(drop.iterdir()):
            if entry.suffix.lower() in (".zip", ".apk") or entry.is_dir():
                out.append(entry)
    return [p for p in out if p.exists()]


def bundle_index(source: Path) -> dict[str, str]:
    """Map bundle id -> member path, for one source."""
    index: dict[str, str] = {}
    if source.is_dir():
        for path in source.rglob("__data"):
            m = CACHE_ENTRY_RE.search(str(path))
            if m:
                index[m.group("bundle")] = str(path)
        for path in source.rglob("*.assetbundle"):
            index.setdefault(path.name.replace(".assetbundle", ""), str(path))
    elif source.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(source) as zf:
                for name in zf.namelist():
                    m = CACHE_ENTRY_RE.search(name)
                    if m:
                        index[m.group("bundle")] = name
        except (OSError, zipfile.BadZipFile):
            pass
    return index


def _read_source(source: Path, member: str) -> bytes | None:
    if source.is_dir():
        try:
            return Path(member).read_bytes()
        except OSError:
            return None
    try:
        with zipfile.ZipFile(source) as zf:
            return zf.read(member)
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


# --------------------------------------------------------------------------
# manifest mapping
# --------------------------------------------------------------------------

def manifest() -> dict[str, str]:
    """bundle id -> path advertised by the Aliases manifest.

    Reading the manifest (rather than guessing paths) is what lets us map a
    cached bundle onto the exact URL the client will request.
    """
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache

    path = config.ROOT / "cdn" / "Aliases" / "rat" / "Android" / "manifest.json"
    mapping: dict[str, str] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for bundle_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if isinstance(url, str) and "/AssetBundles/" in url:
                mapping[bundle_id] = url.split("/AssetBundles/", 1)[1]

    _manifest_cache = mapping
    return mapping


def invalidate() -> None:
    global _manifest_cache
    _manifest_cache = None


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------

def cache_root() -> Path:
    return config.ROOT / "cdn" / "AssetBundles"


def cached(relative: str) -> Path | None:
    target = cache_root() / relative
    if target.is_file() and target.stat().st_size > 0:
        return target
    return None


def locate(relative: str) -> tuple[Path, str] | tuple[None, None]:
    """Find a bundle by its manifest-relative path.

    Returns ``(path_or_none, origin)`` where origin is ``"cache"``, the source
    path, or ``"manifest"`` when we only know it should exist.
    """
    hit = cached(relative)
    if hit:
        return hit, "cache"

    # relative is "<group>/<platform>/<name>.assetbundle"; the bundle id is the
    # file stem, which is what the cache is keyed on.
    name = Path(relative).name
    bundle_id = name[: -len(".assetbundle")] if name.endswith(".assetbundle") else name

    for source in sources():
        index = bundle_index(source)
        member = index.get(bundle_id)
        if member:
            return source, member
    return None, None


def fetch(relative: str) -> bytes | None:
    """Return bundle bytes, caching to disk on first access."""
    hit = cached(relative)
    if hit:
        return hit.read_bytes()

    source, member = locate(relative)
    if source is None:
        return None

    data = _read_source(source, str(member))
    if not data:
        return None
    if not data.startswith(b"UnityFS"):
        # Not a bundle (some caches keep a wrapper); refuse rather than serve
        # something the client cannot parse.
        return None

    with _lock:
        target = cache_root() / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
    return data


def available() -> tuple[int, int]:
    """(#bundles resolvable now, #advertised by the manifest)."""
    mapping = manifest()
    resolvable = 0
    for relative in mapping.values():
        if cached(relative) or locate(relative)[0] is not None:
            resolvable += 1
    return resolvable, len(mapping)


# --------------------------------------------------------------------------
# game data tables
# --------------------------------------------------------------------------

WANTED_TABLES = {
    "ItemInfo", "AttackInfo", "MortyInfo", "PlayerAvatarInfo", "RecipeInfo",
    "MortyAttacksInfo", "RaidBossInfo", "RaidBossAttackInfo", "WorldInfo",
    "QuestInfo", "NPCInfo", "TrainerInfo", "IAPInfo", "ProductInfo",
    "RaidEventInfo", "ArenaRewardsInfo", "ArenaEventInfo",
    "MortySlotsDataInfo", "DeckSlotsConfigsInfo", "BundleAssetAssignment",
    "GachaDefault",
    # localisation: gives every item / Morty / attack its real display name
    "ZH_CN", "ZH_TW", "EN", "JP", "KO",
}

# TextAssets only live in these bundles; scanning all 155 is wasteful.
TABLE_BUNDLES = ("spdata", "mpdata", "appdata", "text")


def gamedata_dir() -> Path:
    return config.ROOT / "data" / "gamedata"


def ensure_gamedata(force: bool = False) -> dict[str, int]:
    """Extract the JSON data tables out of the bundles, once.

    Returns ``{table: entry_count}``.  Requires UnityPy; if it is missing we
    return an empty dict rather than failing, because the server itself does
    not need these tables -- only the admin tools do.
    """
    out = gamedata_dir()
    marker = out / ".complete"

    if marker.is_file() and not force:
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    try:
        import UnityPy  # noqa: F401
    except ImportError:
        return {}

    summary: dict[str, int] = {}
    mapping = manifest()

    for bundle_id in TABLE_BUNDLES:
        relative = mapping.get(bundle_id)
        if not relative:
            continue
        data = fetch(relative)
        if not data:
            continue
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".assetbundle", delete=False) as fh:
            fh.write(data)
            tmp = Path(fh.name)
        try:
            env = UnityPy.load(str(tmp))
            for obj in env.objects:
                if obj.type.name != "TextAsset":
                    continue
                try:
                    asset = obj.read()
                except Exception:  # noqa: BLE001
                    continue
                name = getattr(asset, "m_Name", "") or ""
                if name not in WANTED_TABLES:
                    continue
                raw = asset.m_Script
                raw = (raw.encode("utf-8", "replace") if isinstance(raw, str)
                       else bytes(raw))
                if not raw:
                    continue
                sub = out / ("spdata" if bundle_id == "spdata"
                             else "mpdata" if bundle_id == "mpdata" else "")
                sub.mkdir(parents=True, exist_ok=True)
                (sub / f"{name}.json").write_bytes(raw)
                try:
                    parsed = json.loads(raw)
                    summary[name] = len(parsed) if isinstance(parsed, (dict, list)) else 0
                except ValueError:
                    pass
        finally:
            tmp.unlink(missing_ok=True)

    if summary:
        out.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
