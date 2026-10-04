#!/usr/bin/env python3
"""Pocket Mortys private server -- entry point.

    python3 run.py

Stdlib only: no pip installs, no PHP, no MySQL, no Docker.  State lives in a
SQLite file next to the code.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

# The Windows bundle ships CPython's *embeddable* distribution, which runs in
# isolated mode: sys.path comes from a `._pth` file rather than from the
# environment.  Inserting our own directory explicitly means the server starts
# identically whether it is run from a checkout, a bundle, or a symlink.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Force UTF-8 on the console.  Without this, Windows uses the OEM code page and
# any non-ASCII in a path or a log line raises UnicodeEncodeError mid-request.
os.environ.setdefault("PYTHONUTF8", "1")

from pmnet import cheats, config, db  # noqa: E402


def banner() -> None:
    base = config.public_base()
    line = "  " + "-" * 62
    # Line-buffer stdout: when the launcher's output is piped or redirected,
    # Python block-buffers and the user stares at an empty window wondering
    # whether the server started.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    print()
    print("  Pocket Mortys 私服服务端  /  Pocket Mortys private server")
    print(line)
    print(f"  listening   : {config.BIND_HOST}:{config.BIND_PORT}")
    print(f"  client base : {base}")
    print(f"  database    : {config.RUNTIME_DB}")
    print(f"  recordings  : {config.RECORD_DIR}")
    print(line)
    cheats_active = cheats.describe()
    if cheats_active:
        print("  作弊项已启用 / cheats enabled:")
        for entry in cheats_active:
            print(f"      * {entry}")
        print(line)
    print()
    print("  手机请连接到同一个 Wi-Fi，然后打开游戏。")
    print("  Connect the phone to the same Wi-Fi as this PC, then launch")
    print("  the game.  The APK must already point at the address above:")
    print()
    print(f"      {base}")
    print()
    print("  如果 client base 的 IP 和手机能访问的地址不一致，")
    print("  需要用 tools/build_apk.py 重新生成 APK。")
    print(line)
    print()


def _warn_if_apk_target_stale() -> None:
    """Compare the built APK's baked-in address against this machine's.

    The client has its server address compiled into global-metadata.dat, so a
    DHCP lease change silently breaks an already-installed APK.  tools/build_apk.py
    drops a small marker next to the APK recording what it was pointed at;
    checking it here turns a mystifying "the game just spins" into a one-line
    instruction.
    """
    import json

    marker = config.ROOT / "build" / "apk-target.json"
    if not marker.is_file():
        return
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    # tools/build_enhanced.py 会同时产出好几个变体（通用 127.0.0.1、MuMu
    # 10.0.2.2），所以标记是一个列表：只要有一个命中当前地址就不报警。
    targets = recorded.get("targets")
    if not isinstance(targets, list) or not targets:
        targets = [recorded]

    def host_of(entry: dict) -> str:
        raw = str(entry.get("host") or "")
        return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw

    for entry in targets:
        if not isinstance(entry, dict):
            continue
        if host_of(entry) == config.PUBLIC_HOST and \
                int(entry.get("port") or 0) == config.PUBLIC_PORT:
            return

    described = "、".join(
        f"http://{e.get('host')}:{e.get('port')}"
        for e in targets if isinstance(e, dict)) or "（无）"

    print()
    print("  " + "!" * 58)
    print("  APK 里的服务器地址和本机对不上 / the built APK points elsewhere")
    print(f"      现有 APK 写的是 : {described}")
    print(f"      本机现在是      : {config.public_base()}")
    print()
    print("  如果上面的地址里有你能从设备访问到的那个，忽略本提示即可。")
    print("  否则重新生成一个指向当前地址的 APK：")
    print()
    print("      python3 tools/build_enhanced.py --variant full \\")
    print(f"          --metadata-host <设备能访问到的地址> \\")
    print("          --out build/enhanced/PocketMortys-加强版-通用.apk")
    print()
    print("  MuMu 模拟器建议直接用 10.0.2.2（宿主机固定别名，不用建隧道）。")
    print("  " + "!" * 58)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=config.BIND_HOST)
    ap.add_argument("--port", type=int, default=config.BIND_PORT)
    ap.add_argument("--reinit", action="store_true",
                    help="rebuild the live database from the legacy dump "
                         "(destroys all players)")
    ap.add_argument("--routes", action="store_true",
                    help="print every registered route and exit")
    args = ap.parse_args()

    # The database lives next to the code, so the package must be extracted
    # somewhere writable.  This bites people who unzip into Program Files,
    # where the obvious symptom is an opaque sqlite3 "unable to open database
    # file" much later.  Catch it up front and say what to do.
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.DATA_DIR / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        print()
        print(f"  数据目录不可写 / data directory is not writable:")
        print(f"      {config.DATA_DIR}")
        print(f"  {exc}")
        print()
        print("  请把整个文件夹解压到可写的位置（例如桌面或下载目录），")
        print("  不要放在 C:\\Program Files 下。")
        print("  Please extract the folder somewhere writable, e.g. the")
        print("  Desktop or Downloads -- not under C:\\Program Files.")
        print()
        return 1

    try:
        db.bootstrap(force=args.reinit)
    except SystemExit as exc:
        print()
        print(f"  {exc}")
        return 1

    from pmnet import http as pmhttp
    import pmnet.routes  # noqa: F401  (side-effect: registers endpoints)

    if args.routes:
        for method, path in pmhttp.all_routes():
            print(f"{method:<6} {path}")
        return 0

    config.BIND_HOST = args.host
    config.BIND_PORT = args.port

    handler = pmhttp.Handler_
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        # By far the most common failure in the field is "port already in use",
        # usually because a previous window is still open.  Say so plainly
        # instead of dumping a traceback at someone who just double-clicked.
        print()
        print(f"  无法在 {args.host}:{args.port} 启动 / cannot bind "
              f"{args.host}:{args.port}")
        print(f"  {exc}")
        print()
        print("  端口可能已被占用（比如上次的服务端窗口还开着）。")
        print("  换一个端口试试 / try another port:")
        print()
        print(f"      python run.py --port {args.port + 1}")
        print()
        return 1
    server.daemon_threads = True

    banner()
    print(f"  {len(pmhttp.all_routes())} routes registered")
    _warn_if_apk_target_stale()
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
