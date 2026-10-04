#!/usr/bin/env python3
"""给官方 Pocket Mortys 客户端打上「过无网络」和「过隐私协议」补丁。

适用对象
--------
官方发布包 `Pocket.Mortys.V2.41.0.apk`（Project-Pocket-Mortys/.github 的
release，下载量 10 万+）里的 `lib/arm64-v8a/libil2cpp.so`。

这个 .so 和社区服务器仓库里那份**逐字节相同**
（sha256 ff773be7…c8e4326，52,190,824 字节），所以偏移对两者都适用。

五个补丁点
----------
1. 过隐私协议  @ 0x16839a4
       cbz  w8, #0x16839bc     →  b  #0x16839c0
   原逻辑：读一个标志位，为 0 就跳到弹隐私协议的分支。改成无条件跳转，
   直接跳过弹窗继续初始化。

2. 过无网络    @ 0x16517fc
       mov  w0, w21            →  mov  w0, #1
   这是 `AssetBundleState.CheckForInternetConnection()` 的返回值。
   dump.cs 里的签名：
       // RVA: 0x1655694  Offset: 0x1651694  VA: 0x1655694
       public static bool CheckForInternetConnection() { }
   （文件偏移 = VA − 0x4000）

   它读的是 Unity 的 `Application.internetReachability`，而后者来自 Android
   系统的网络验证状态。系统拿 HTTP 打 Google 的 generate_204 做验证，宿主机
   只要有 VPN 劫持默认路由就过不去，网络被标记「未验证」，Unity 报告
   NotReachable。游戏于是：

       · 完全走本地缓存，一个请求都不发给服务器
       · 联网功能弹「无法连接至网络。请查看你的网络连接并重试。」

   改成恒返回 true 之后它才会去连服务器。

3~5. 三个资源失败标志  @ 0x147f678 / 0x147f68c / 0x147f6a0
       ldrb w0, [x0, #0x48]    →  mov  w0, wzr
       ldrb w0, [x0, #0x49]    →  mov  w0, wzr
       ldrb w0, [x0, #0x4a]    →  mov  w0, wzr

   分别对应 `AssetBundleController` 的 didLocalFail / didServerFail /
   didServerStoredFail。它们为真时会弹「游戏资源无法读取，请升级至最新版本
   的口袋莫蒂」并拒绝继续。恒返回 false 后无论加载怎么失败都继续。

用法
----
    python3 patch_official_client.py <libil2cpp.so> [-o 输出.so]
    python3 patch_official_client.py <libil2cpp.so> --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# 指令编码（小端）
MOV_W0_ZERO = bytes.fromhex("e0031f2a")   # mov w0, wzr
MOV_W0_ONE  = bytes.fromhex("20008052")   # mov w0, #1
B_UNCOND    = bytes.fromhex("06000014")   # b #0x16839bc（0x16839a4 + 6*4）
CBZ_W8      = bytes.fromhex("c8000034")   # cbz w8, #0x16839bc

# 把整个函数体压成 `mov w0, #1; ret`
RET_TRUE    = MOV_W0_ONE + bytes.fromhex("c0035fd6")

# 三个方法各自的函数序言（用来确认没打错位置）
PROLOG_20   = bytes.fromhex("fe0f1ef8") + bytes.fromhex("f44f01a9")   # str x30,[sp,#-0x20]! / stp x20,x19
PROLOG_10   = bytes.fromhex("fe0f1ff8") + bytes.fromhex("083440f9")   # str x30,[sp,#-0x10]! / ldr x8,[x0,#0x68]


# (名称, 文件偏移, 期望的原字节, 替换字节, 说明)
PATCHES = [
    # ── 过隐私协议 / 过无网络 ────────────────────────────────────────
    ("过隐私协议", 0x16839a4, CBZ_W8, B_UNCOND,
     "cbz → b，跳过隐私协议弹窗"),
    ("过无网络",   0x16517fc, bytes.fromhex("e003152a"), MOV_W0_ONE,
     "CheckForInternetConnection() 恒返回 true"),

    # ── 资源加载失败标志（避免「资源无法读取」弹窗）─────────────────
    ("资源标志1",  0x147f678, bytes.fromhex("00204139"), MOV_W0_ZERO,
     "didLocalFail 恒 false"),
    ("资源标志2",  0x147f68c, bytes.fromhex("00244139"), MOV_W0_ZERO,
     "didServerFail 恒 false"),
    ("资源标志3",  0x147f6a0, bytes.fromhex("00284139"), MOV_W0_ZERO,
     "didServerStoredFail 恒 false"),

    # ── 跳过新手教程 ─────────────────────────────────────────────────
    #
    # 脚本里本来就有 TestDefs.DebugSkipTutorial()，但那是给开发者命令行
    # 用的，正常流程进不去。真正决定「这段剧情播过没有」的是
    # PlayerDefs.GetStorySeen(id) —— 开场动画（GarageIntro）、车库教学、
    # 各维度的首次进入提示，全都走它。
    #
    # 让它恒返回 true，等于「所有剧情都看过了」，于是开场直接跳过。
    ("跳过剧情",   0x173c888, PROLOG_20, RET_TRUE,
     "PlayerDefs.GetStorySeen() 恒 true → 开场/教程剧情全部跳过"),
    ("教程完成",   0x14a97f8, PROLOG_20, RET_TRUE,
     "SaveDataController.IsCampaignTutorialComplete() 恒 true"),
    ("多人解锁",   0x14a97cc, PROLOG_10, RET_TRUE,
     "SaveDataController.IsMultiplayerUnlocked() 恒 true"),

    # ── OneTrust 死循环 ──────────────────────────────────────────────
    #
    # OneTrust 隐私 SDK 拿不到配置时，`OneTrustController.<UpdateSDKs>b__34_0()`
    # 会在 `UnityMainThreadDispatcher.Update()` 里每帧抛
    # NullReferenceException，游戏永远卡在加载转圈：
    #
    #     at OneTrust.CMPSDK.get_domainData_Groups ()
    #     at OneTrustController.<UpdateSDKs>b__34_0 ()
    #     at UnityMainThreadDispatcher.Update ()
    #
    # 正常做法是让服务器把 /bannersdk/v2/applicationdata 返回真实的 116 KB 配置。
    # 但如果那一步没打通（明文被拦、域名没换干净等），这个方法就是死循环的源头。
    # 它是 void 无参的，把函数体压成一条 ret 即可彻底止住。
    ("OneTrust",   0x1490980, bytes.fromhex("ffc301d1"), bytes.fromhex("c0035fd6"),
     "OneTrustController.<UpdateSDKs>b__34_0() 直接返回 → 止住每帧空引用死循环"),

    # ── 多人暂停菜单崩溃 ────────────────────────────────────────────
    #
    # 进多人模式的暂停界面时 `MPPauseUI.SetLabels()` 会直接崩掉客户端
    # （本地化表在多人上下文里没被加载，取 label 时空引用）。
    # 它是 void 无参的，压成一条 ret 即可。
    ("MP暂停",     0x15cc980, bytes.fromhex("fe57bea9"), bytes.fromhex("c0035fd6"),
     "MPPauseUI.SetLabels() 直接返回 → 不再崩溃"),
]

# 校验输入文件身份
EXPECT_SIZE = 52190824
KNOWN_SHA256 = "ff773be7d3718ee307f6342358268748e8fa2487460e7b3857715478cb8e4326"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sofile", type=Path, help="libil2cpp.so 路径")
    ap.add_argument("-o", "--output", type=Path, help="输出路径（默认原地，先备份 .orig）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不修改")
    ap.add_argument("--skip-verify", action="store_true", help="跳过文件身份校验")
    args = ap.parse_args()

    blob = bytearray(args.sofile.read_bytes())
    print(f"  文件       {args.sofile}")
    print(f"  大小       {len(blob):,} 字节")

    if len(blob) != EXPECT_SIZE:
        print(f"  ⚠️  大小和已知版本不符（期望 {EXPECT_SIZE:,}）")
        if not args.skip_verify:
            print("      如果确认是这个版本的 .so，加 --skip-verify 继续")
            return 1

    if not args.skip_verify:
        import hashlib
        h = hashlib.sha256(blob).hexdigest()
        if h == KNOWN_SHA256:
            print(f"  身份       ✓ 官方 V2.41.0 / 社区版（sha256 匹配）")
        else:
            print(f"  身份       ⚠️ sha256 不匹配")
            print(f"             实际 {h}")
            print(f"             已知 {KNOWN_SHA256}")
            print("             继续尝试按偏移打补丁…")

    print()
    print("  ── 补丁点")
    ok = skip = fail = 0
    for name, off, expect, repl, note in PATCHES:
        n = len(expect)                      # 补丁长度可变（4 或 8 字节）
        cur = bytes(blob[off:off + n])
        tag = f"0x{off:x}"

        if cur == repl:
            print(f"    ─ {name:<12} {tag:<12} 已是补丁状态")
            skip += 1
            continue

        if cur != expect:
            print(f"    ✗ {name:<12} {tag:<12} 字节不符")
            print(f"        期望 {expect.hex(' ')}   实际 {cur.hex(' ')}")
            fail += 1
            continue

        if args.dry_run:
            print(f"    · {name:<12} {tag:<12} {cur.hex(' ')} → {repl.hex(' ')}   {note}")
            ok += 1
            continue

        blob[off:off + n] = repl
        print(f"    ✓ {name:<12} {tag:<12} {cur.hex(' ')} → {repl.hex(' ')}   {note}")
        ok += 1

    print()
    if args.dry_run:
        print(f"  （试运行）将修改 {ok} 处，跳过 {skip} 处，失败 {fail} 处")
        return 0

    if ok == 0:
        print("  没有需要修改的地方。")
        return 0 if fail == 0 else 1

    out = args.output or args.sofile
    if args.output is None:
        backup = args.sofile.with_suffix(args.sofile.suffix + ".orig")
        if not backup.exists():
            shutil.copy2(args.sofile, backup)
            print(f"  已备份到   {backup}")
    out.write_bytes(bytes(blob))
    print(f"  已写入     {out}（改了 {ok} 处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
