#!/usr/bin/env python3
"""构建《口袋莫蒂》私服「加强版」客户端 APK —— 一条命令、可复现、自带校验。

为什么要有这个脚本
------------------
之前几个 APK 是手工拼的，结果互相覆盖、互相回退，出现了这些真实缺陷：

* ``PocketMortys-完全版.apk``   —— 只剩 8 个 .so 补丁，丢了
  ``globalgamemanagers`` 里的 ``insecureHttpOption``（=2 才允许明文 HTTP），
  于是 Unity 层直接拒绝所有 ``http://``，游戏一个请求都发不出去。
* ``PocketMortys-自包含版.apk`` —— 资源塞进去了，但 .so 只打了 2 个补丁、
  同样丢了 ``insecureHttpOption``、``network_security_config.xml`` 更是
  从头到尾没换成过。
* ``1-成品/network_security_config.xml`` —— 名字写着「成品」，
  其实是**纯文本 XML**（798 字节），直接塞进 APK 会让资源编译失败、
  游戏启动即崩：``Resources$NotFoundException: Corrupt XML binary file``。

所以这里把每一处改动都写成显式步骤，并在最后统一复验。

改动清单
--------
1. ``lib/arm64-v8a/libil2cpp.so``                 10 处 arm64 指令补丁
2. ``assets/bin/Data/Managed/Metadata/global-metadata.dat``
                                                  服务器地址（长度不变）
3. ``assets/bin/Data/globalgamemanagers``         ``insecureHttpOption = 2``
4. ``res/xml/network_security_config.xml``        编译好的二进制 XML，
                                                  放行明文 HTTP
5. ``assets/AssetBundle.dat``                     预置资源清单（版本 1011）
6. ``assets/AssetBundles/Android/**``             （``--variant full``）
                                                  157 条目资源全部内置

用法
----
    # 真机 / 模拟器通用（metadata 指向 127.0.0.1，配合 adb reverse）
    python3 tools/build_enhanced.py --variant full \\
        --out build/enhanced/PocketMortys-加强版-通用.apk

    # MuMu 模拟器专用：10.0.2.2 与 127.0.0.1 同为 14 字节，可以原地替换，
    # 这样连 adb reverse 都不用
    python3 tools/build_enhanced.py --variant full --metadata-host 10.0.2.2 \\
        --out build/enhanced/PocketMortys-加强版-MuMu.apk

    # 只校验一个已存在的 APK
    python3 tools/build_enhanced.py --verify build/enhanced/xxx.apk
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "tools"))
from patch_official_client import PATCHES as SO_PATCHES  # noqa: E402

# --------------------------------------------------------------------------
# 常量：来自逆向结果，改动前请先读 docs/ 里的说明
# --------------------------------------------------------------------------

#: ``globalgamemanagers`` 里 ``insecureHttpOption`` 的字节偏移。
#: 0 = NotAllowed（Unity 拒绝明文 HTTP），2 = AlwaysAllowed。
GGM_INSECURE_HTTP_OPTION_OFFSET = 209724
GGM_INSECURE_HTTP_OPTION_ALLOW = 2

#: 已知能用的 metadata（社区验证版，服务器地址已指向 127.0.0.1:8080）
KNOWN_GOOD_METADATA = ROOT / "client-patch" / "1-成品" / "global-metadata-社区验证版.dat"
COMMUNITY_METADATA_DEFAULT_HOST = "127.0.0.1:8080"

#: 资源清单 + 包体
ASSET_MANIFEST_SRC = ROOT / "community-pure" / "Aliases" / "rat" / "Android" / "manifest.json"
ASSET_BUNDLE_DAT = ROOT / "client-data" / "AssetBundle.dat"
ASSET_BUNDLE_DIR = (ROOT / "community-pure" / "AssetBundles"
                    / "AssetBundle-android_1011-ios_1011__7b175320" / "Android")

KEYSTORE = ROOT / "build" / "pmnet.keystore"
KS_PASS = os.environ.get("PMNET_KS_PASS", "pmnet123")

BT_CANDIDATES = [
    Path.home() / "Library" / "Android" / "sdk" / "build-tools" / "36.0.0",
    Path.home() / "Library" / "Android" / "sdk" / "build-tools" / "36.1.0",
    Path.home() / "Library" / "Android" / "sdk" / "build-tools" / "34.0.0",
]
ANDROID_JAR_CANDIDATES = sorted(
    (Path.home() / "Library" / "Android" / "sdk" / "platforms").glob("android-*/android.jar")
) if (Path.home() / "Library" / "Android" / "sdk" / "platforms").is_dir() else []




def run(argv, **kw):
    """调用外部程序。Windows 上 .bat/.cmd 必须经 cmd /c，否则 CreateProcess 起不来。"""
    argv = [str(a) for a in argv]
    if os.name == "nt" and argv and argv[0].lower().endswith((".bat", ".cmd")):
        argv = ["cmd", "/c", *argv]
    return subprocess.run(argv, **kw)


def tool(bt: Path, name: str) -> str:
    """在 build-tools 目录里找可执行文件，兼容 Windows 的 .exe / .bat。

    Windows 上 build-tools 里是 ``aapt2.exe`` / ``d8.bat`` / ``zipalign.exe`` /
    ``apksigner.bat``，Linux/macOS 上则是无后缀的。硬编码无后缀名在 Windows
    上会直接 FileNotFoundError，所以统一走这里。
    """
    for suffix in (".exe", ".bat", ".cmd", ""):
        cand = bt / (name + suffix)
        if cand.is_file():
            return str(cand)
    return str(bt / name)


def build_tools() -> Path:
    for p in BT_CANDIDATES:
        if (p / "aapt2").is_file() and (p / "apksigner").is_file():
            return p
    sys.exit("找不到 Android build-tools（需要 aapt2 / zipalign / apksigner）")


def android_jar() -> Path:
    if not ANDROID_JAR_CANDIDATES:
        sys.exit("找不到 android.jar（需要 platforms/android-XX）")
    return ANDROID_JAR_CANDIDATES[-1]


# --------------------------------------------------------------------------
# 步骤 1：libil2cpp.so
# --------------------------------------------------------------------------

def patch_so(data: bytearray) -> list[str]:
    done: list[str] = []
    for name, off, expect, repl, _note in SO_PATCHES:
        n = len(expect)
        cur = bytes(data[off:off + n])
        if cur == repl:
            done.append(f"{name}(已就位)")
            continue
        if cur != expect:
            raise SystemExit(
                f"libil2cpp.so 补丁点 {name} @ {hex(off)} 字节不符："
                f"期望 {expect.hex(' ')}，实际 {cur.hex(' ')}")
        data[off:off + n] = repl
        done.append(name)
    return done


# --------------------------------------------------------------------------
# 步骤 2：global-metadata.dat
# --------------------------------------------------------------------------

def patch_metadata(data: bytearray, host: str | None, port: int) -> str:
    """把 metadata 里的服务器地址换成 ``host:port``（**必须等长**）。

    为什么坚持等长
    --------------
    metadata 里描述字符串长度的地方不止一处：数据区里每个字面量前面有
    7-bit 前缀，另外还有若干索引表也存着长度。等长替换一个字段都不用动，
    才能真正做到「不可能改坏」。社区那份验证过能用的 metadata 就是这么来的。

    比 ``127.0.0.1:8080`` 短的地址用 ``/`` 补到同样长度：多出来的斜杠落在
    主机与路径之间，变成 ``http://10.0.2.2:8080//Aliases/...``。这在 HTTP 里
    是合法路径，服务端的 ``_normalize_path`` 会把连续斜杠折叠掉
    （见 ``pmnet/http.py``）。比它长的地址请改用 ``adb reverse``。
    """
    canonical = COMMUNITY_METADATA_DEFAULT_HOST          # "127.0.0.1:8080"
    target = canonical if host is None else (
        host if port in (80, 443) else f"{host}:{port}")
    if target == canonical:
        return canonical
    if len(target) > len(canonical):
        raise SystemExit(
            f"metadata 等长替换只支持不超过 {len(canonical)} 字节的地址，"
            f"{target!r} 有 {len(target)} 字节。\n"
            f"  真机走 Wi-Fi 请改用 adb reverse：APK 保持 127.0.0.1，"
            f"由 tools/launch.py 建隧道。")
    padded = target + "/" * (len(canonical) - len(target))
    data[:] = bytes(data).replace(canonical.encode(), padded.encode())
    if padded.encode() not in bytes(data):
        raise SystemExit("替换没生效，metadata 来源文件不对")
    return target


# --------------------------------------------------------------------------
# 步骤 3：globalgamemanagers
# --------------------------------------------------------------------------

def patch_ggm(data: bytearray) -> tuple[int, int]:
    off = GGM_INSECURE_HTTP_OPTION_OFFSET
    before = data[off]
    data[off] = GGM_INSECURE_HTTP_OPTION_ALLOW
    return before, data[off]


# --------------------------------------------------------------------------
# 步骤 4：network_security_config.xml（编译成二进制 XML）
# --------------------------------------------------------------------------

NSC_XML = """<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="true">
        <trust-anchors>
            <certificates src="system" />
            <certificates src="user" />
        </trust-anchors>
    </base-config>
    <debug-overrides>
        <trust-anchors>
            <certificates src="system" />
            <certificates src="user" />
        </trust-anchors>
    </debug-overrides>
</network-security-config>
"""

_MANIFEST_XML = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
                 'package="tmp.nsc"></manifest>\n')


def dump_binary_xml(nsc_blob: bytes, manifest_blob: bytes) -> str:
    """把一段二进制 XML 塞进一个最小合法 APK，再用 aapt2 反解出来。

    ``aapt2 dump`` 只认完整 APK（必须有 AndroidManifest.xml）；
    单独给一个 XML 文件，它会报 "could not identify format of APK"。
    """
    bt = build_tools()
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.apk"
        with zipfile.ZipFile(probe, "w") as zf:
            zf.writestr("AndroidManifest.xml", manifest_blob)
            zf.writestr("res/xml/network_security_config.xml", nsc_blob)
        res = run(
            [tool(bt, "aapt2"), "dump", "xmltree", "--file",
             "res/xml/network_security_config.xml", str(probe)],
            capture_output=True, text=True)
        return res.stdout or res.stderr


def compile_nsc(out_dir: Path) -> Path:
    """用 aapt2 把 NSC 编译成真正的二进制 XML 并校验。

    直接照抄文本 XML 进 APK 是之前踩过的坑：APK 照样打得开，
    但游戏一启动就 ``Resources$NotFoundException: Corrupt XML binary file``。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "network_security_config.xml"
    stamp = out_dir / ".source.sha256"
    digest = hashlib.sha256(NSC_XML.encode()).hexdigest()
    if target.is_file() and stamp.is_file() and stamp.read_text().strip() == digest:
        return target

    bt = build_tools()
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "res" / "xml").mkdir(parents=True)
        (work / "res" / "xml" / "network_security_config.xml").write_text(
            NSC_XML, encoding="utf-8")
        (work / "AndroidManifest.xml").write_text(_MANIFEST_XML, encoding="utf-8")
        run([tool(bt, "aapt2"), "compile", "--dir", "res",
                        "-o", "compiled.zip"], cwd=work, check=True)
        run([tool(bt, "aapt2"), "link", "-o", "out.apk",
                        "-I", str(android_jar()), "--manifest", "AndroidManifest.xml",
                        "compiled.zip"], cwd=work, check=True)
        with zipfile.ZipFile(work / "out.apk") as zf:
            blob = zf.read("res/xml/network_security_config.xml")
            probe_manifest = zf.read("AndroidManifest.xml")
        if not blob.startswith(b"\x03\x00\x08\x00"):
            sys.exit("aapt2 输出的不是二进制 XML，拒绝使用")
        target.write_bytes(blob)

    # 用 aapt2 反解一次，确认真的读得出来、且明文确实放行了
    dump = dump_binary_xml(target.read_bytes(), probe_manifest)
    if "base-config" not in dump or "cleartextTrafficPermitted" not in dump:
        sys.exit(f"编译出来的 NSC 校验失败：\n{dump}")
    stamp.write_text(digest)
    return target


# --------------------------------------------------------------------------
# 步骤 5/6：重新打包
# --------------------------------------------------------------------------

def repack(src: Path, dst: Path, replacements: dict[str, Path],
           additions: dict[str, Path], drop_signature: bool = True) -> dict:
    """只替换/新增指定条目，其余原样拷贝（含压缩方式）。

    ``resources.arsc`` 必须保持 STORED，否则 Android 12+ 直接拒绝安装：
    ``Targeting R+ ... requires the resources.arsc ... stored uncompressed``。
    """
    replaced, added = [], []
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
            dst, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zout:
        names = set(zin.namelist())
        for key, path in replacements.items():
            if key not in names:
                raise SystemExit(f"原 APK 里没有 {key}，无法替换")
        for item in zin.infolist():
            name = item.filename
            if drop_signature and (name.startswith("META-INF/")
                                   and name.upper().endswith((".RSA", ".DSA", ".EC", ".SF"))
                                   or name == "META-INF/MANIFEST.MF"):
                continue
            data = replacements[name].read_bytes() if name in replacements \
                else zin.read(name)
            store = (item.compress_type == zipfile.ZIP_STORED
                     or name == "resources.arsc"
                     or name.endswith(".arsc"))
            zi = zipfile.ZipInfo(name, date_time=item.date_time)
            zi.compress_type = zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED
            zi.external_attr = item.external_attr
            zi.internal_attr = item.internal_attr
            zi.create_system = item.create_system
            zout.writestr(zi, data)
            if name in replacements:
                replaced.append((name, item.file_size, len(data)))

        existing = set(zout.namelist())
        for name, path in sorted(additions.items()):
            if name in existing:
                continue
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zout.writestr(zi, path.read_bytes())
            added.append((name, path.stat().st_size))
    return {"replaced": replaced, "added": added}


def align_and_sign(apk: Path) -> None:
    bt = build_tools()
    aligned = apk.with_suffix(".aligned.apk")
    run([tool(bt, "zipalign"), "-f", "-p", "4", str(apk), str(aligned)],
                   check=True)
    shutil.move(str(aligned), str(apk))
    run([
        tool(bt, "apksigner"), "sign",
        "--ks", str(KEYSTORE),
        "--ks-pass", f"pass:{KS_PASS}",
        "--key-pass", f"pass:{KS_PASS}",
        "--ks-key-alias", "pmnet",
        "--v2-signing-enabled", "true",
        "--v3-signing-enabled", "true",
        str(apk),
    ], check=True)


# --------------------------------------------------------------------------
# 复验
# --------------------------------------------------------------------------

def verify(apk: Path) -> bool:
    ok = True
    print(f"\n  ── 复验 {apk}")
    bt = build_tools()
    with zipfile.ZipFile(apk) as zf:
        names = set(zf.namelist())

        def entry_size(name):
            return zf.getinfo(name).file_size

        # 1. .so
        so = bytearray(zf.read("lib/arm64-v8a/libil2cpp.so"))
        bad = []
        for name, off, _expect, repl, _note in SO_PATCHES:
            cur = bytes(so[off:off + len(repl)])
            if cur != repl:
                bad.append(name)
        print(f"     libil2cpp.so        {len(so):,} 字节  "
              f"{'✅ 10 处补丁齐' if not bad else '❌ 缺 ' + ','.join(bad)}")
        ok &= not bad

        # 2. metadata
        md = zf.read("assets/bin/Data/Managed/Metadata/global-metadata.dat")
        hosts = {h: md.count(h.encode()) for h in
                 ("127.0.0.1", "10.0.2.2", "192.168", "bps-pmnet.com",
                  "conspiracyrick.com")}
        print(f"     metadata            {len(md):,} 字节  {hosts}")
        ok &= len(md) == 11144902
        ok &= hosts["bps-pmnet.com"] < 18   # 原版是 18

        # 3. globalgamemanagers
        ggm = zf.read("assets/bin/Data/globalgamemanagers")
        val = ggm[GGM_INSECURE_HTTP_OPTION_OFFSET]
        print(f"     globalgamemanagers  insecureHttpOption = {val}  "
              f"{'✅ 允许明文' if val == 2 else '❌ 明文会被 Unity 拒绝'}")
        ok &= val == 2

        # 4. NSC
        nsc = zf.read("res/xml/network_security_config.xml")
        dump = dump_binary_xml(nsc, zf.read("AndroidManifest.xml"))
        cleartext = "cleartextTrafficPermitted=true" in dump
        print(f"     network_security    {len(nsc)} 字节  "
              f"{'✅ cleartext=true' if cleartext else '❌ 没有 base-config'}")
        ok &= cleartext

        # 5. 资源
        bundles = [n for n in names if n.startswith("assets/AssetBundles/Android/")
                   and n.endswith(".assetbundle")]
        has_dat = "assets/AssetBundle.dat" in names
        has_manifest = "assets/AssetBundles/Android/manifest.json" in names
        # 清单版本必须和客户端缓存里那份一致，否则启动就报「过期资源」。
        # 这里同时把两边解析出来对比 —— 上一版 APK 就是在这个点上翻车的。
        ver = count = None
        if has_manifest:
            try:
                doc = json.loads(zf.read("assets/AssetBundles/Android/manifest.json"))
                ver = doc.get("version")
                count = len([k for k in doc if k not in ("assetBundleTags", "version")])
            except ValueError:
                ver = "解析失败"
        print(f"     StreamingAssets     {len(bundles)} 个包  "
              f"AssetBundle.dat={'有' if has_dat else '无'}  "
              f"manifest version={ver} 条目={count}")

        dat_ver = None
        if has_dat:
            raw = zf.read("assets/AssetBundle.dat")
            # .NET BinaryFormatter 布局（见 4-放文件教程/踩坑记录.txt 坑 8）：
            #   ... 06 00 00 00 | <version i32> | 06 03 00 00 00 | <7-bit JSON 长度> | JSON
            idx = raw.find(b"\x06\x03\x00\x00\x00", 200)
            if idx > 4:
                dat_ver = struct.unpack_from("<i", raw, idx - 4)[0]
        print(f"     AssetBundle.dat     内嵌 LoadedManifestVersion = {dat_ver}")

        # 自包含版：内置清单必须和种子清单完全一致，否则客户端直接判「过期资源」。
        # 精简版不内置包体，走「种子清单 + 启动脚本推 UnityCache」那条
        # （也就是 10-01/10-02 实测能跑通的那套），所以只做提示不做断言。
        if len(bundles) >= 150:
            if not isinstance(count, int) or count < 150:
                print("     ❌ 自包含版内置清单不完整")
                ok = False
            if dat_ver is not None and isinstance(ver, int) and dat_ver != ver:
                print(f"     清单版本 {ver} 与种子 {dat_ver} 不同 —— 客户端会去服务端取真清单")
        else:
            print("     精简版：不内置包体，首次运行需由 tools/launch.py 推 UnityCache")

        # 6. 结构
        arsc = zf.getinfo("resources.arsc")
        stored = arsc.compress_type == zipfile.ZIP_STORED
        signed = any(n.startswith("META-INF/") and n.upper().endswith(".RSA")
                     for n in names)
        print(f"     resources.arsc      {'✅ STORED' if stored else '❌ 被压缩了'}"
              f"   签名 {'✅ v1/v2' if signed else '—'}")
        ok &= stored

    res = run([tool(bt, "apksigner"), "verify", "--print-certs", str(apk)],
                         capture_output=True, text=True)
    print(f"     apksigner verify    {'✅ 通过' if res.returncode == 0 else '❌ 失败'}")
    ok &= res.returncode == 0
    return ok


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path,
                    default=ROOT / "client" / "Pocket.Mortys.V2.41.0.apk",
                    help="官方原版 APK（默认 client/Pocket.Mortys.V2.41.0.apk）")
    ap.add_argument("--manifest-version", type=int,
                    help="覆盖内置 StreamingAssets 清单的 version（自包含版调试用）")
    ap.add_argument("--variant", choices=("lite", "full"), default="full",
                    help="lite = 不含资源包（约 175MB）；full = 全部内置（约 360MB）")
    ap.add_argument("--metadata", type=Path, default=KNOWN_GOOD_METADATA,
                    help="已指向 127.0.0.1:8080 的 metadata 基准文件")
    ap.add_argument("--metadata-host", help="把 metadata 改成这个主机（必须等长）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--out", type=Path, required=False)
    ap.add_argument("--verify", type=Path, help="只复验一个已存在的 APK")
    ap.add_argument("--skip-sign", action="store_true")
    args = ap.parse_args()

    if args.verify:
        return 0 if verify(args.verify) else 1

    if not args.out:
        ap.error("--out 是必须的")

    for p in (args.base, args.metadata):
        if not p.is_file():
            sys.exit(f"缺少输入文件：{p}")

    work = Path(tempfile.mkdtemp(prefix="pmbuild-"))
    print(f"  基准 APK  {args.base}  ({args.base.stat().st_size:,} 字节)")
    print(f"  变体      {args.variant}")

    # ── 1. .so ──
    with zipfile.ZipFile(args.base) as zf:
        so = bytearray(zf.read("lib/arm64-v8a/libil2cpp.so"))
        ggm = bytearray(zf.read("assets/bin/Data/globalgamemanagers"))
    patched = patch_so(so)
    so_path = work / "libil2cpp.so"
    so_path.write_bytes(bytes(so))
    print(f"  libil2cpp.so          打好 {len(patched)} 处补丁")

    # ── 2. metadata ──
    md = bytearray(args.metadata.read_bytes())
    endpoint = patch_metadata(md, args.metadata_host, args.port)
    md_path = work / "global-metadata.dat"
    md_path.write_bytes(bytes(md))
    print(f"  metadata              指向 http://{endpoint}")

    # ── 3. globalgamemanagers ──
    before, after = patch_ggm(ggm)
    ggm_path = work / "globalgamemanagers"
    ggm_path.write_bytes(bytes(ggm))
    print(f"  globalgamemanagers    insecureHttpOption {before} → {after}")

    # ── 4. NSC ──
    nsc_path = compile_nsc(ROOT / "build" / "enhanced" / "nsc")
    print(f"  network_security      {nsc_path.stat().st_size} 字节（aapt2 编译 + 反解校验通过）")

    # ── 5/6. 资源 ──
    replacements = {
        "lib/arm64-v8a/libil2cpp.so": so_path,
        "assets/bin/Data/Managed/Metadata/global-metadata.dat": md_path,
        "assets/bin/Data/globalgamemanagers": ggm_path,
        "res/xml/network_security_config.xml": nsc_path,
    }
    additions: dict[str, Path] = {}

    if ASSET_BUNDLE_DAT.is_file():
        additions["assets/AssetBundle.dat"] = ASSET_BUNDLE_DAT
        print(f"  AssetBundle.dat        {ASSET_BUNDLE_DAT.stat().st_size:,} 字节（预置清单）")

    # 首启自愈（Java 层）：把内置资源铺成客户端期望的本地形态。
    # 需要 base APK 里已经注入 com.pmseed.SeedApplication（见 tools/build_java_layer.py）。
    java_layer = ROOT / "build" / "enhanced" / "classes4.dex"
    if java_layer.is_file() and args.variant == "full":
        additions["classes4.dex"] = java_layer
        idx = work / "index.txt"
        lines = []
        mani = json.loads(ASSET_MANIFEST_SRC.read_text(encoding="utf-8"))
        for key, entry in mani.items():
            if not isinstance(entry, dict):
                continue
            ver = int(entry.get("version", 1))
            lines.append(f"{key} {'0' * 24}{struct.pack('<I', ver & 0xFFFFFFFF).hex()}")
        idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
        additions["assets/pmseed/index.txt"] = idx
        print(f"  首启自愈层             classes4.dex + index（{len(lines)} 条）")

    if args.variant == "full":
        if not ASSET_MANIFEST_SRC.is_file() or not ASSET_BUNDLE_DIR.is_dir():
            sys.exit("找不到资源包目录，--variant full 需要 community-pure/AssetBundles")
        manifest_src = ASSET_MANIFEST_SRC
        if args.manifest_version is not None:
            doc = json.loads(ASSET_MANIFEST_SRC.read_text(encoding="utf-8"))
            doc["version"] = str(args.manifest_version)
            manifest_src = work / "manifest.json"
            manifest_src.write_text(
                json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
            print(f"  内置清单 version      {args.manifest_version}"
                  f"（低于服务端 → 客户端会去服务端取真清单并落盘）")
        additions["assets/AssetBundles/Android/manifest.json"] = manifest_src
        bundles = sorted(ASSET_BUNDLE_DIR.glob("*.assetbundle"))
        total = 0
        for b in bundles:
            additions[f"assets/AssetBundles/Android/{b.name}"] = b
            total += b.stat().st_size
        print(f"  资源包                 {len(bundles)} 个 / {total:,} 字节 全部内置")

    # 原始 APK 里已经有 ``assets/AssetBundles/Android/manifest.json``（官方那份
    # 只有 72 字节、version=1）和 ``text.assetbundle``。**必须先转成替换**，
    # 否则会被当成「已存在，跳过」，客户端读到的仍是官方空壳清单 ——
    # 实测后果就是启动时报「无法连接…错误：过期资源」。
    with zipfile.ZipFile(args.base) as zf:
        in_base = set(zf.namelist())
    for name in list(additions):
        if name in in_base:
            replacements[name] = additions.pop(name)
    print(f"  覆盖既有条目           {len(replacements)} 个"
          f"（含 manifest.json / text.assetbundle）")

    # ── 打包 ──
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    info = repack(args.base, out, replacements, additions)
    print(f"  已重打包               {len(info['replaced'])} 替换 / "
          f"{len(info['added'])} 新增  → {out.stat().st_size:,} 字节")

    if not args.skip_sign:
        align_and_sign(out)
        print(f"  zipalign + apksigner   完成  → {out.stat().st_size:,} 字节")

    # 服务端用来判断「APK 里烧的地址是不是本机」的标记。
    # 会同时存在好几个变体（通用 127.0.0.1 / MuMu 10.0.2.2），所以存成一个列表，
    # 只要命中其中一个就不报警。
    marker = ROOT / "build" / "apk-target.json"
    entry = {"host": endpoint, "port": args.port, "variant": args.variant,
             "apk": out.name, "built": time.strftime("%Y-%m-%d %H:%M:%S")}
    existing: dict = {}
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    targets = [t for t in existing.get("targets", []) if isinstance(t, dict)]
    targets = [t for t in targets if t.get("apk") != out.name] + [entry]
    marker.write_text(json.dumps(
        {"targets": targets, "host": entry["host"], "port": args.port,
         "variant": args.variant, "apk": out.name},
        ensure_ascii=False, indent=2), encoding="utf-8")

    shutil.rmtree(work, ignore_errors=True)
    good = verify(out)
    print(f"\n  {'✅ 加强版构建完成' if good else '❌ 复验未通过'}  {out}")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
