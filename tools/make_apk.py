#!/usr/bin/env python3
"""交互式生成加强版 APK —— macOS / Windows 共用一个入口。

``一键生成APK.command``（macOS 双击）与 ``一键生成APK.bat``（Windows 双击）
都只是这个脚本的壳。所有中文输出都在 Python 里，避免 .bat 被 cmd.exe 按
OEM 代码页解析导致乱码（这是上一版踩过的坑，.bat 必须纯 ASCII）。

用法
----
    python3 tools/make_apk.py                        # 交互式，问两个问题
    python3 tools/make_apk.py --check                # 只体检，不生成
    python3 tools/make_apk.py --base 官方.apk --host 192.168.1.100 -o 输出.apk
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
MANIFEST = ROOT / "server" / "cdn" / "Aliases" / "rat" / "Android" / "manifest.json"
SEED_DAT = ROOT / "server" / "cdn" / "AssetBundle.dat"

#: 官方 V2.41.0 的 libil2cpp.so 身份。偏移是对着这一份定的，别的版本打不上。
SO_SIZE = 52_190_824
SO_SHA256 = "ff773be7d3718ee307f6342358268748e8fa2487460e7b3857715478cb8e4326"
KNOWN_APK_SIZE = 174_924_654


def say(msg: str = "") -> None:
    print(msg, flush=True)


def sdk_dir() -> pathlib.Path | None:
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            p = pathlib.Path(os.environ[env])
            if p.is_dir():
                return p
    cands = [
        pathlib.Path.home() / "Library" / "Android" / "sdk",          # macOS
        pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk",  # Windows
        pathlib.Path.home() / "Android" / "Sdk",                       # Linux
    ]
    for p in cands:
        if p.is_dir():
            return p
    return None


def check() -> list[str]:
    """返回体检结果（每行一条）。任何 ❌ 都意味着跑不下去。"""
    out: list[str] = []
    out.append(f"  Python      {sys.version.split()[0]}   {sys.executable}")
    py_ok = sys.version_info >= (3, 8)
    out.append(f"  {'✅' if py_ok else '❌'} Python >= 3.8")

    javac = shutil.which("javac")
    java = shutil.which("java")
    out.append(f"  {'✅' if javac else '❌'} JDK（javac）{'  ' + javac if javac else '  —— 装 JDK 17+，或 brew install openjdk'}")

    apktool = shutil.which("apktool") or shutil.which("apktool.bat")
    out.append(f"  {'✅' if apktool else '❌'} apktool  {apktool or '—— brew install apktool / 或下 apktool.jar + apktool.bat'}")

    sdk = sdk_dir()
    bt = jar = None
    if sdk:
        bts = sorted((sdk / "build-tools").glob("*")) if (sdk / "build-tools").is_dir() else []
        bt = bts[-1] if bts else None
        jars = sorted((sdk / "platforms").glob("android-*/android.jar")) if (sdk / "platforms").is_dir() else []
        jar = jars[-1] if jars else None
    out.append(f"  {'✅' if bt else '❌'} Android SDK build-tools  {bt or '—— 缺 aapt2 / zipalign / apksigner'}")
    out.append(f"  {'✅' if jar else '❌'} android.jar  {jar or '—— sdkmanager \"platforms;android-34\"'}")
    out.append(f"  {'✅' if MANIFEST.is_file() else '❌'} 资源清单  {MANIFEST}")
    out.append(f"  {'✅' if java else '⚠️'} java 运行时  {java or '（apksigner 需要）'}")
    return out


def inspect_base(path: pathlib.Path) -> bool:
    """确认这是 V2.41.0 官方包。偏移是对着它定的，别的版本打不上。"""
    if not path.is_file():
        say(f"  ❌ 找不到文件：{path}")
        return False
    size = path.stat().st_size
    if size != KNOWN_APK_SIZE:
        say(f"  ⚠️ 大小 {size:,} 字节，官方 V2.41.0 是 {KNOWN_APK_SIZE:,} 字节")
        say("     如果确实是自己改过的包，可以继续；不然请重新下载官方原版。")
    try:
        with zipfile.ZipFile(path) as z:
            so = z.read("lib/arm64-v8a/libil2cpp.so")
    except Exception as exc:                                    # noqa: BLE001
        say(f"  ❌ 读不了这个 APK：{exc}")
        return False
    digest = hashlib.sha256(so).hexdigest()
    if len(so) != SO_SIZE or digest != SO_SHA256:
        say(f"  ❌ libil2cpp.so 不是预期的那一份")
        say(f"       实际 {len(so):,} 字节  sha256 {digest[:32]}…")
        say(f"       期望 {SO_SIZE:,} 字节  sha256 {SO_SHA256[:32]}…")
        say("     10 处补丁的偏移只对官方 V2.41.0 有效，请换回原版。")
        return False
    say(f"  ✅ 官方 V2.41.0 校验通过（libil2cpp.so sha256 匹配）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=pathlib.Path, help="官方 APK 路径")
    ap.add_argument("--host", help="服务器地址，设备能访问到的（如 192.168.1.100）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("-o", "--out", type=pathlib.Path, default=ROOT / "PocketMortys-加强版.apk")
    ap.add_argument("--check", action="store_true", help="只体检环境，不生成")
    ap.add_argument("--work", type=pathlib.Path, default=pathlib.Path("/tmp/pmbase"))
    args = ap.parse_args()

    say()
    say("  " + "=" * 62)
    say("   口袋莫蒂 加强版 · 生成器")
    say("  " + "=" * 62)
    say()
    say("  ── 环境体检")
    for line in check():
        say(line)
    say()

    if args.check:
        return 0

    sdk = sdk_dir()
    ok = sdk and (shutil.which("javac") or "") and (shutil.which("apktool") or shutil.which("apktool.bat"))
    if not ok:
        say("  ❌ 环境不齐，先按上面的 ❌ 装好再跑。macOS：")
        say("       brew install apktool openjdk")
        say("     然后装 Android SDK 的 build-tools 与 platforms（Android Studio 或 cmdline-tools）")
        return 1
    if not SEED_DAT.is_file():
        say(f"  ❌ 缺少 {SEED_DAT}")
        say("     这是首启自愈要铺的完整资源清单，仓库里带在 server/cdn/ 下。")
        return 1

    base = args.base
    while base is None:
        raw = input("  官方 APK 的路径（拖进来也行）: ").strip().strip('"').strip("'")
        if raw:
            base = pathlib.Path(raw)
    say()
    if not inspect_base(base):
        return 1

    host = args.host
    while not host:
        host = input("\n  服务器地址（设备能访问到的，如 192.168.1.100）: ").strip()
    say()

    work = args.work
    say("  [1/2] 生成带首启自愈层的基准包…（要几分钟）")
    r1 = subprocess.run([
        sys.executable, str(HERE / "build_java_layer.py"),
        "--base", str(base), "--work", str(work),
        "--manifest", str(MANIFEST), "--dat", str(SEED_DAT),
        "-o", str(work.parent / "base-java.apk"),
    ])
    if r1.returncode != 0:
        say("\n  ❌ 第一步失败。常见原因：apktool 没装 / JDK 版本太低 / 磁盘空间不够。")
        return r1.returncode

    say("\n  [2/2] 打全部客户端补丁 + 内置 155 个资源包…（要几分钟）")
    r2 = subprocess.run([
        sys.executable, str(HERE / "build_enhanced.py"),
        "--base", str(work.parent / "base-java.apk"),
        "--variant", "full",
        "--metadata-host", host, "--port", str(args.port),
        "--out", str(args.out),
    ])
    if r2.returncode != 0:
        say("\n  ❌ 第二步失败，看上面的复验输出。")
        return r2.returncode

    say()
    say("  " + "=" * 62)
    say(f"   ✅ 生成完毕：{args.out}")
    say(f"      大小 {args.out.stat().st_size:,} 字节")
    say("  " + "=" * 62)
    say()
    say("   装到设备（签名与官方不同，必须先卸载）：")
    say("       adb uninstall com.conspiracyrick.pocketmortys")
    say(f'       adb install -g "{args.out}"')
    say()
    say("   首次启动慢约 1 分钟 —— APK 内置的引导代码正在把 155 个资源包")
    say("   铺进 files/UnityCache/（179 MB）。别杀进程，之后每次秒进。")
    say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
