#!/usr/bin/env python3
"""一键把《口袋莫蒂》加强版跑起来：起服务端 → 连设备 → 建隧道 → 装包 → 启动。

它解决的问题
------------
以前每次玩都要手动做这些事：

    adb connect 127.0.0.1:7555
    adb root && adb reverse tcp:8080 tcp:8080     # adb root 之后还得再来一次
    adb push AssetBundle.dat /sdcard/Android/data/.../files/
    adb push UnityCache      /sdcard/Android/data/.../files/
    adb shell chmod -R 777 ...
    adb shell chcon -R u:object_r:media_rw_data_file:s0:...
    python3 run.py

现在一条命令（或双击 ``一键开始.command``）全做完，并且每一步都会
先检测再做，不重复劳动。

用法
----
    python3 tools/launch.py                 # 自动判断：先找 MuMu，再找 USB 真机
    python3 tools/launch.py --scenario mumu
    python3 tools/launch.py --scenario usb --apk build/enhanced/xxx.apk
    python3 tools/launch.py --wifi 192.168.1.23:5555
    python3 tools/launch.py --push-cache     # 顺便把资源缓存推进去（精简版用）
    python3 tools/launch.py --status         # 只看状态，不动任何东西
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "com.conspiracyrick.pocketmortys"
ACTIVITY = f"{PACKAGE}/com.unity3d.player.UnityPlayerActivity"
REMOTE_FILES = f"/sdcard/Android/data/{PACKAGE}/files"

ADB_CANDIDATES = [
    Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
    ROOT / ".android-sdk" / "platform-tools" / "adb",
    Path("/opt/homebrew/bin/adb"),
    Path("/usr/local/bin/adb"),
]
MUMU_ADB_PORTS = (7555, 16384, 5555)

ENHANCED_DIR = ROOT / "build" / "enhanced"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    say(f"  [{n}/{total}] {msg}")


def find_adb() -> str | None:
    for p in ADB_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which("adb")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_port(port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not port_free(port):
            return True
        time.sleep(0.3)
    return False


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------

class Adb:
    def __init__(self, exe: str):
        self.exe = exe

    def raw(self, *args: str) -> subprocess.CompletedProcess:
        return run([self.exe, *args])

    def devices(self) -> list[str]:
        res = self.raw("devices")
        out = []
        for line in res.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                out.append(parts[0])
        return out

    def shell(self, serial: str, cmd: str) -> subprocess.CompletedProcess:
        return self.raw("-s", serial, "shell", cmd)

    def install(self, serial: str, apk: Path) -> bool:
        res = self.raw("-s", serial, "install", "-r", "-g", str(apk))
        text = res.stdout + res.stderr
        if "Success" in text:
            return True
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in text or "signatures do not match" in text:
            say("      ⚠️ 签名和机上的不一样（多半是旧的官方/社区版）。")
            say(f"      ⚠️ 必须卸载重装；卸载会清掉本机存档 Player.dat。")
            res = self.raw("-s", serial, "uninstall", PACKAGE)
            say(f"      卸载：{(res.stdout + res.stderr).strip()}")
            res = self.raw("-s", serial, "install", "-r", "-g", str(apk))
            text = res.stdout + res.stderr
            return "Success" in text
        say(f"      ❌ 安装失败：{text.strip()[:400]}")
        return False

    def installed_version(self, serial: str) -> str:
        res = self.shell(serial, f"dumpsys package {PACKAGE} | grep -m1 versionName")
        return (res.stdout or "").strip()

    def root(self, serial: str) -> bool:
        """尽量拿 root。

        模拟器上 ``/sdcard`` 是 FUSE，非 root 身份往别的应用目录里写会被拒
        （``Permission denied``），而游戏数据目录正属于「别的应用」。
        真机没 root 也没关系，退化成普通 adb 身份，后面按需报错。
        """
        res = self.raw("-s", serial, "root")
        text = (res.stdout + res.stderr).lower()
        if "cannot run as root" in text or "adbd cannot" in text:
            return False
        if "restarting" in text:
            # adbd 重启后设备会短暂 offline，等它回来
            self.raw("-s", serial, "wait-for-device")
            time.sleep(1.5)
        return True


MUMU_TOOL = Path("/Applications/MuMuPlayer.app/Contents/MacOS/mumutool")

#: 游戏真正会读的「资源清单缓存」。
#:
#: 这一条是实测出来的关键：APK 内置的 155 个资源包会被客户端读进 UnityCache，
#: 但 ``files/AssetBundle.dat`` 如果由游戏自己新建，它写出来的是一份 226 字节的
#: 空壳（只有版本号、没有清单字符串），客户端随即判定「过期资源」并停在错误弹窗。
#: 把这份带完整清单的 31,826 字节文件推过去，游戏才有得比对。
#: 只有 31 KB，秒传。
SEED_MANIFEST = ROOT / "client-data" / "AssetBundle.dat"


def mumu_devices() -> list[dict]:
    """问 MuMu 自带的 CLI 要实例列表 —— 比猜 adb 端口靠谱得多。

    ``mumutool info all`` 的形状是 ``{"return": {"count": n, "results": [...]}}``，
    ``mumutool info <n>`` 则是 ``{"return": {...}}``。两种都要能吃下。
    """
    if not MUMU_TOOL.is_file():
        return []
    res = run([str(MUMU_TOOL), "info", "all"])
    raw = res.stdout or ""
    start = raw.find("{")
    if start < 0:
        return []
    try:
        doc = json.loads(raw[start:])
    except ValueError:
        return []
    ret = doc.get("return")
    if isinstance(ret, list):
        return [d for d in ret if isinstance(d, dict)]
    if isinstance(ret, dict):
        if isinstance(ret.get("results"), list):
            return [d for d in ret["results"] if isinstance(d, dict)]
        return [ret]
    return []


def connect_mumu(adb: Adb) -> str | None:
    """连 MuMu。优先问 CLI 要端口，问不到再退回常见端口表。

    模拟器的 adb 连接挺容易掉（装完包、``adb root`` 之后都掉过），
    所以看到 ``offline`` 就先断开重连一次，再认它。
    """
    for dev in mumu_devices():
        port = dev.get("adb_port")
        if dev.get("state") == "running" and port:
            adb.raw("connect", f"127.0.0.1:{port}")
            for serial in adb.devices():
                if serial.endswith(f":{port}"):
                    return serial
            adb.raw("disconnect", f"127.0.0.1:{port}")
            adb.raw("connect", f"127.0.0.1:{port}")
            for serial in adb.devices():
                if serial.endswith(f":{port}"):
                    return serial
    for port in MUMU_ADB_PORTS:
        adb.raw("connect", f"127.0.0.1:{port}")
        for serial in adb.devices():
            if serial.endswith(f":{port}"):
                return serial
    return None


def wait_for_device(adb: Adb, scenario: str, wifi: str | None,
                    seconds: int = 60) -> tuple[str | None, str]:
    """等设备出现。返回 (serial, 实际场景)。"""
    end = time.time() + seconds
    attempt = 0
    while True:
        attempt += 1
        if wifi:
            adb.raw("connect", wifi)
            if any(s == wifi for s in adb.devices()):
                return wifi, "wifi"
        if scenario in ("auto", "mumu"):
            serial = connect_mumu(adb)
            if serial:
                return serial, "mumu"
        if scenario in ("auto", "usb", "wifi"):
            devs = adb.devices()
            if devs:
                return devs[0], ("usb" if scenario == "auto" else scenario)
        if time.time() >= end:
            return None, scenario
        if attempt % 4 == 0:
            say("      … 还在等设备（模拟器要开机完成后 adb 才通）")
        time.sleep(3)


# --------------------------------------------------------------------------
# 服务端
# --------------------------------------------------------------------------

class Server:
    def __init__(self, public_host: str, port: int, log: Path):
        self.public_host = public_host
        self.port = port
        self.log = log
        self.proc: subprocess.Popen | None = None

    def start(self) -> subprocess.Popen:
        env = dict(os.environ)
        env["PMNET_PUBLIC_HOST"] = self.public_host
        env["PMNET_PORT"] = str(self.port)
        env["PYTHONUTF8"] = "1"
        # 资源包来源：community-pure 那套完整的 155 个包。
        # pmnet.assets 会按需解出来缓存到 cdn/AssetBundles，不用预先铺文件。
        sources = [
            str(ROOT / "community-pure" / "AssetBundles"
                / "AssetBundle-android_1011-ios_1011__7b175320" / "Android"),
            str(ROOT / "data" / "sources"),
        ]
        env["PMNET_ASSET_SOURCES"] = os.pathsep.join(sources)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("", encoding="utf-8")
        handle = self.log.open("a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "run.py"), "--port", str(self.port)],
            cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True)
        return self.proc

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except OSError:
                self.proc.terminate()


# --------------------------------------------------------------------------
# 选 APK
# --------------------------------------------------------------------------

def pick_apk(scenario: str, explicit: Path | None) -> Path | None:
    if explicit:
        return explicit if explicit.is_file() else None
    names = {
        "mumu": ["PocketMortys-加强版-MuMu.apk", "PocketMortys-加强版-通用.apk"],
        "usb": ["PocketMortys-加强版-通用.apk", "PocketMortys-加强版-MuMu.apk"],
        "wifi": ["PocketMortys-加强版-通用.apk"],
    }.get(scenario, ["PocketMortys-加强版-通用.apk"])
    for name in names:
        p = ENHANCED_DIR / name
        if p.is_file():
            return p
    # 退而求其次：目录里任意一个加强版
    for p in sorted(ENHANCED_DIR.glob("PocketMortys-加强版-*.apk")):
        return p
    return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=("auto", "mumu", "usb", "wifi"),
                    default="auto")
    ap.add_argument("--wifi", help="无线调试地址，例如 192.168.1.23:5555")
    ap.add_argument("--apk", type=Path)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--public-host",
                    help="服务端对外公布的地址（默认按场景自动选）")
    ap.add_argument("--no-install", action="store_true")
    ap.add_argument("--no-launch", action="store_true")
    ap.add_argument("--push-cache", action="store_true",
                    help="把 UnityCache 与 AssetBundle.dat 推到设备（精简版建议加）")
    ap.add_argument("--no-consent", action="store_true",
                    help="不要自动点掉首启的条款弹窗")
    ap.add_argument("--status", action="store_true", help="只报告状态，不做任何事")
    ap.add_argument("--keep-alive", action="store_true",
                    help="前台守着服务端日志（双击脚本时默认开启）")
    args = ap.parse_args()

    total = 8 if not args.status else 1
    say()
    say("  " + "=" * 62)
    say("   《口袋莫蒂》加强版 · 一键启动")
    say("  " + "=" * 62)
    say()

    # ── adb ──
    adb_exe = find_adb()
    adb = Adb(adb_exe) if adb_exe else None

    # ── 选设备 ──
    scenario = args.scenario
    serial = None
    if adb and not args.status:
        serial, scenario = wait_for_device(adb, scenario, args.wifi)
    elif adb:
        if args.wifi:
            adb.raw("connect", args.wifi)
            serial = args.wifi
        elif scenario in ("auto", "mumu"):
            serial = connect_mumu(adb)
            if serial:
                scenario = "mumu"
        if not serial and scenario in ("auto", "usb"):
            devs = adb.devices()
            if devs:
                serial = devs[0]
                scenario = "usb"

    public_host = args.public_host or ("10.0.2.2" if scenario == "mumu" else "127.0.0.1")
    apk = pick_apk(scenario, args.apk)

    if args.status:
        say(f"  adb        : {adb_exe or '未找到'}")
        say(f"  场景       : {scenario}")
        say(f"  设备       : {serial or '未连接'}")
        say(f"  APK        : {apk or '未找到（先跑 tools/build_enhanced.py）'}")
        say(f"  服务端地址 : http://{public_host}:{args.port}")
        say(f"  端口占用   : {'否（可以启动）' if port_free(args.port) else '是（服务端已在跑？）'}")
        say()
        return 0

    # ── 1. 服务端 ──
    log = ROOT / "logs" / f"launch-{time.strftime('%Y%m%d-%H%M%S')}.log"
    server = Server(public_host, args.port, log)
    if port_free(args.port):
        step(1, total, f"启动服务端（对外公布 http://{public_host}:{args.port}）")
        server.start()
        if not wait_port(args.port, 20):
            say(f"      ❌ 服务端起不来，看日志：{log}")
            say(log.read_text(encoding='utf-8')[-1500:])
            return 1
        say(f"      ✅ 已在 0.0.0.0:{args.port} 监听，日志 {log}")
    else:
        step(1, total, f"端口 {args.port} 已被占用，当作服务端已在运行")
        server = None

    # ── 2. 设备 ──
    step(2, total, "连接设备")
    if not adb:
        say("      ❌ 找不到 adb。装 Android platform-tools，或设 ADB 环境变量。")
        return 1
    if not serial:
        say("      ⚠️ 没找到设备。MuMu 请先打开模拟器；真机请插上 USB 并允许调试。")
        if server is not None:
            say(f"      ⚠️ 本脚本刚起的服务端留着没停（{log}），设备连上后重跑即可。")
        if args.keep_alive and server is not None:
            tail_forever(log, server)
        return 1
    ver = adb.installed_version(serial)
    say(f"      ✅ {serial}" + (f"   已装 {ver.splitlines()[-1] if ver else ''}" if ver else "   （还没装游戏）"))
    # adb root 会让 adbd 重启、隧道失效，所以必须在建隧道【之前】做
    if adb.root(serial):
        say("      — 已切到 root（模拟器上推文件需要）")
        adb.raw("connect", serial)

    # ── 3. 隧道 ──
    step(3, total, "建立 adb reverse 隧道（设备的 127.0.0.1:端口 → 本机）")
    if public_host in ("127.0.0.1", "localhost"):
        adb.raw("-s", serial, "reverse", f"tcp:{args.port}", f"tcp:{args.port}")
        listed = adb.raw("-s", serial, "reverse", "--list").stdout
        ok = f"tcp:{args.port}" in listed
        say(f"      {'✅ 隧道就绪' if ok else '⚠️ 隧道没建上，检查 adb'}  {listed.strip()}")
    else:
        # MuMu 走 10.0.2.2，本机对它来说就是宿主机，不需要隧道
        say(f"      — 跳过：APK 里烧的是 {public_host}，模拟器可直接访问宿主机")

    # ── 4. 装包 ──
    step(4, total, "安装客户端")
    if args.no_install:
        say("      — 按参数要求跳过")
    elif not apk:
        say("      ⚠️ 没找到加强版 APK，跳过安装。先生成：")
        say("         python3 tools/build_enhanced.py --variant full "
            "--out build/enhanced/PocketMortys-加强版-通用.apk")
    else:
        say(f"      用 {apk.name}（{apk.stat().st_size / 1048576:.1f} MB）")
        if adb.install(serial, apk):
            say("      ✅ 已安装")
        else:
            return 1

    # ── 5. 资源缓存 ──
    step(5, total, "资源缓存（UnityCache + 资源清单）")
    if args.push_cache:
        push_cache(adb, serial)
    else:
        ensure_cache(adb, serial)
        push_seed(adb, serial)
        say("      （资源包是 APK 自带的，这里只补一份 31 KB 的清单）")

    # ── 6. 启动游戏 ──
    step(6, total, "启动游戏")
    if args.no_launch:
        say("      — 按参数要求跳过")
    else:
        adb.shell(serial, f"am force-stop {PACKAGE}")
        res = adb.shell(serial, f"am start -n {ACTIVITY}")
        out = (res.stdout + res.stderr).strip()
        say(f"      {'✅ 已拉起' if 'Starting' in out or 'Activity' in out else out}")

    # ── 7. 自动点掉首启的条款弹窗 ──
    step(7, total, "处理首次启动的「使用条款」弹窗")
    if args.no_launch or args.no_consent:
        say("      — 按参数要求跳过")
    else:
        say("      （按屏幕比例自动点「接受」；如果没弹，它只是在空点，无副作用）")
        threading.Thread(target=accept_consent, args=(adb, serial),
                         daemon=True).start()

    # ── 8. 看日志 ──
    step(8, total, "服务端日志（Ctrl-C 停止服务端）")
    tail_forever(log, server)
    return 0


def fix_permissions(adb: Adb, serial: str) -> None:
    """属主 / 权限 / SELinux 标签，三者缺一游戏都读不到文件。"""
    adb.raw("-s", serial, "shell", f"chmod -R 777 {REMOTE_FILES}")
    adb.raw("-s", serial, "shell", f"chown -R 10289:10289 {REMOTE_FILES}")
    adb.raw("-s", serial, "shell",
            f"chcon -R u:object_r:media_rw_data_file:s0:c47,c256,c512,c768 {REMOTE_FILES}")


def count_cached(adb: Adb, serial: str) -> int:
    res = adb.shell(serial, f"ls {REMOTE_FILES}/UnityCache/Shared 2>/dev/null | wc -l")
    txt = (res.stdout or "0").strip().split()
    for tok in reversed(txt):
        if tok.isdigit():
            return int(tok)
    return 0


def ensure_cache(adb: Adb, serial: str) -> bool:
    """把 155 个资源包铺进 UnityCache —— 全靠客户端自己从 APK 里读。

    为什么分两步
    ------------
    实测出来的顺序问题，很反直觉：

    * 客户端**没有** ``files/AssetBundle.dat`` 时，会把 APK 内置的
      ``assets/AssetBundles/Android/*.assetbundle`` 全部读出来、写进
      ``files/UnityCache/``（155 个真实包，179 MB），然后自己生成一份
      226 字节的空壳清单，报「过期资源」。
    * 直接先把完整清单塞进去，客户端反而认为「资源我都有了」，不去读
      APK 里的包，等到真要用 ``appdata`` 时才会发现缓存是空的，
      报「无法连接…错误：appdata」。

    所以顺序必须是：先让它自己铺缓存 → 再补上完整清单 → 重启。
    全程只有 31 KB 的推送，不用推 179 MB。
    """
    have = count_cached(adb, serial)
    if have >= 150:
        say(f"      — UnityCache 里已有 {have} 个包，跳过铺设")
        return True

    say("      首次运行：让客户端自己把 APK 内置的资源铺开（约 1 分钟）…")
    adb.shell(serial, f"rm -f {REMOTE_FILES}/AssetBundle.dat")
    adb.shell(serial, f"am force-stop {PACKAGE}")
    adb.shell(serial, f"am start -n {ACTIVITY}")
    have = 0
    for i in range(30):
        time.sleep(5)
        have = count_cached(adb, serial)
        if have and have % 40 < 5:
            say(f"        … 已铺开 {have} 个")
        if have >= 150:
            break
    adb.shell(serial, f"am force-stop {PACKAGE}")
    say(f"      {'✅ 铺开完成：' + str(have) + ' 个包' if have >= 150 else '⚠️ 只铺开 ' + str(have) + ' 个，可能要看一眼模拟器'}")
    return have >= 150


def push_seed(adb: Adb, serial: str) -> bool:
    """推资源清单缓存 —— 游戏能过「过期资源」判定的必要文件，只有 31 KB。

    走 ``/data/local/tmp`` 中转：模拟器的 ``/sdcard`` 是 FUSE，
    直接 push 到别的应用的数据目录经常被拒（``Permission denied``），
    先落到 root 可写的中转目录、再用 shell 拷过去，最稳。
    """
    if not SEED_MANIFEST.is_file():
        say(f"      ⚠️ 找不到 {SEED_MANIFEST}，跳过（游戏多半会报「过期资源」）")
        return False

    want = SEED_MANIFEST.stat().st_size
    cur = adb.shell(serial, f"stat -c %s {REMOTE_FILES}/AssetBundle.dat 2>/dev/null")
    if str(want) in (cur.stdout or ""):
        say("      — 设备上那份清单大小已正确，跳过")
        return True

    adb.raw("-s", serial, "shell", "mkdir -p /data/local/tmp/pmseed")
    stage = "/data/local/tmp/pmseed/AssetBundle.dat"
    res = adb.raw("-s", serial, "push", str(SEED_MANIFEST), stage)
    if not (res.returncode == 0 and "pushed" in (res.stdout + res.stderr)):
        say(f"      ⚠️ 中转推送失败：{(res.stderr or res.stdout).strip()[:200]}")
        return False

    adb.raw("-s", serial, "shell", f"mkdir -p {REMOTE_FILES}")
    # 先放开目录权限，再拷贝，再纠正属主/标签
    adb.raw("-s", serial, "shell", f"chmod 777 {REMOTE_FILES}")
    cp = adb.raw("-s", serial, "shell",
                 f"cp -f {stage} {REMOTE_FILES}/AssetBundle.dat")
    if cp.returncode != 0 or "denied" in (cp.stdout + cp.stderr).lower():
        say(f"      ⚠️ 拷贝到应用目录失败：{(cp.stderr or cp.stdout).strip()[:200]}")
        return False

    fix_permissions(adb, serial)
    back = adb.shell(serial, f"stat -c %s {REMOTE_FILES}/AssetBundle.dat")
    got = (back.stdout or "").strip()
    ok = got == str(want)
    say(f"      {'✅ 已写入完整清单（%d 字节）' % want if ok else '⚠️ 写进去的是 ' + got + ' 字节，期望 ' + str(want)}")
    return ok


def accept_consent(adb: Adb, serial: str, tries: int = 12) -> None:
    """自动点掉启动时的「使用条款 / 隐私政策」弹窗。

    这个弹窗不给过就进不了主菜单，而它的「接受」按钮位置随分辨率变化，
    所以按屏幕比例点：横向正中、纵向约 81%。
    """
    size = adb.shell(serial, "wm size")
    w = h = 0
    for tok in (size.stdout or "").replace("Physical size:", " ").replace("x", " ").split():
        if tok.isdigit():
            if not w:
                w = int(tok)
            else:
                h = int(tok)
    if not (w and h):
        w, h = 854, 480
    # 该机型是横屏游戏，但 wm size 报的是竖屏物理尺寸
    landscape_w, landscape_h = max(w, h), min(w, h)
    x, y = int(landscape_w * 0.5), int(landscape_h * 0.81)
    for _ in range(tries):
        time.sleep(6)
        adb.shell(serial, f"input tap {x} {y}")


def push_cache(adb: Adb, serial: str) -> None:
    src_cache = ROOT / "client-data" / "UnityCache"
    if not SEED_MANIFEST.is_file():
        say(f"      ⚠️ 找不到 {SEED_MANIFEST}")
    else:
        adb.raw("-s", serial, "shell", f"mkdir -p {REMOTE_FILES}")
        adb.raw("-s", serial, "push", str(SEED_MANIFEST),
                f"{REMOTE_FILES}/AssetBundle.dat")
    if src_cache.is_dir():
        say("      推进 UnityCache（311 个文件，可能要一会儿）…")
        adb.raw("-s", serial, "push", str(src_cache), f"{REMOTE_FILES}/")
    fix_permissions(adb, serial)
    say("      ✅ 已推送并修好权限（注意：adb root 会让 reverse 失效，脚本已重连）")


def tail_forever(log: Path, server: Server | None) -> None:
    say()
    say("  " + "-" * 62)
    say("  正常的话，下面会依次出现：")
    say("      GET  /generate_204      → 204")
    say("      GET  /time              → 200")
    say("      GET  /Aliases/rat/Android → 200")
    say("      GET  /AssetBundles/**   → 200")
    say("      POST /user/register     → 201")
    say("  看不到任何请求 = 客户端还没连上（检查隧道 / APK 里的地址）。")
    say("  " + "-" * 62)
    say()
    if server is None:
        return
    pos = 0
    try:
        while True:
            if log.is_file():
                with log.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
            if server.proc and server.proc.poll() is not None:
                say("\n  ⚠️ 服务端退出了。")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("\n  停止服务端…")
        server.stop()
        say("  已停止。")


if __name__ == "__main__":
    sys.exit(main())
