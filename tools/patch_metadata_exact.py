#!/usr/bin/env python3
"""把 IL2CPP metadata 里的服务器域名换成任意主机 —— 精确重写版。

为什么要重写一遍
----------------
现有的两个工具各有毛病：

* ``patch_metadata_simple.py`` 只做字节替换，用 ``/`` 补齐。补齐出来的斜杠
  会成为 URL 的一部分（``http://127.0.0.1:8080//////time``），把
  ``parse_url()`` 之类的路径解析搞坏；更要命的是它**不改长度前缀**，
  .NET 会把 NUL 也读进字符串里，报 ``Invalid URI: Invalid port specified``。
* ``patch_metadata_localhost.py`` 会整表重建，把字符串搬到文件尾部。
  能用，但动的地方太多，风险大。

这份实现走中间路线：**原地改写，但不留垃圾、不留错误长度**。

metadata 里每个字符串字面量的真实布局
--------------------------------------
    <7-bit 变长前缀> <UTF-8 字节> <NUL 填充>

前缀的值是 **UTF-16 字节数**，也就是 ``2 × 字符数``（不是 UTF-8 字节数）。
因此：

1. 新串必须 ``≤ 原串分配空间``；
2. 前缀要改成 ``2 × 新串长度``；
3. 剩余空间用 ``\\x00`` 填 —— 前缀已经说清楚长度了，
   填充字节永远不会被读到。

社区那份「验证过能用」的 metadata（``global-metadata-社区验证版.dat``）
就是这么做的。本脚本在 ``--selftest`` 下会从官方原版重放它，
逐字节比对，确认实现和它对得上。

用法
----
    # 试运行
    python3 patch_metadata_exact.py <原版 metadata> --host 192.168.1.42 --port 8080

    # 真改
    python3 patch_metadata_exact.py <原版 metadata> --host 192.168.1.42 --port 8080 \
        --apply -o out.dat

    # 自检：从官方原版重放社区验证版
    python3 patch_metadata_exact.py <官方原版> --selftest <社区验证版>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 要改写的完整主机（前缀带 scheme，顺便把 https 降级成 http）
HOST_PATTERNS = [
    rb"https?://(?:[A-Za-z0-9_-]+\.)*bps-pmnet\.com",
    rb"https?://(?:[A-Za-z0-9_-]+\.)*conspiracyrick\.com",
    rb"https?://\{0\}\.game\.bps-pmnet\.com",
    rb"https?://\{0\}\.game\.conspiracyrick\.com",
]


# --------------------------------------------------------------------------
# 7-bit 变长整数（ECMA-335 compressed unsigned int）
# --------------------------------------------------------------------------

def read_uleb128(data: bytes, pos: int) -> tuple[int, int, int] | None:
    """从 ``pos`` 起读一个 7-bit 变长整数。

    返回 ``(值, 起始位置, 字节数)``；读不动返回 ``None``。

    注意位序：ECMA-335 的压缩整数是**低 7 位在前**，和 protobuf 的
    LEB128 一致，但和 IL2CPP 里另外几处「高 7 位在前」的写法不同。
    这里的判断依据是重放社区版的逐字节结果，不是文档。
    """
    value = 0
    shift = 0
    for i in range(4):
        if pos + i >= len(data):
            return None
        byte = data[pos + i]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos, i + 1
        shift += 7
    return None


def write_uleb128(value: int, width: int) -> bytes:
    """按 ``width`` 字节写出 7-bit 变长整数（宽度必须够）。"""
    out = bytearray()
    for i in range(width):
        byte = (value >> (7 * i)) & 0x7F
        if i != width - 1:
            byte |= 0x80
        out.append(byte)
    if (value >> (7 * width)) != 0:
        raise ValueError(f"{value} 放不进 {width} 字节")
    return bytes(out)


# --------------------------------------------------------------------------
# 改写
# --------------------------------------------------------------------------

class Skip(Exception):
    """这一处不改。"""


def patch(data: bytes, host: str, port: int) -> tuple[bytes, list[dict]]:
    endpoint = host if port in (80, 443) else f"{host}:{port}"
    base = f"http://{endpoint}".encode()
    out = bytearray(data)
    edits: list[dict] = []

    rx = re.compile(b"|".join(HOST_PATTERNS))
    for m in rx.finditer(data):
        start = m.start()
        host_bytes = m.group()
        try:
            # 前缀紧贴在字符串前面，向前找出它的起始字节
            p = start - 1
            if p < 0:
                raise Skip
            steps = 0
            while p >= 0 and data[p] & 0x80:
                p -= 1
                steps += 1
                if steps > 3:
                    raise Skip
            if p < 0:
                raise Skip
            decoded = read_uleb128(data, p)
            if decoded is None:
                raise Skip
            prefix_value, prefix_pos, prefix_len = decoded
            if prefix_pos + prefix_len != start:
                raise Skip          # 前缀后面还有别的东西，判断不了边界
            if prefix_value % 2:
                raise Skip          # 不是 2×字符数，说明押错了
            space = prefix_value // 2
            if space <= 0 or start + space > len(data):
                raise Skip
            original = data[start:start + space]
            if not original.startswith(b"http"):
                raise Skip          # 命中的不是字符串开头
            if not original.startswith(host_bytes):
                raise Skip          # 前缀描述的串和正则匹配的范围对不上

            # 主机之后的部分（路径、查询串）原样保留
            suffix = original[len(host_bytes):]
            new_text = base + suffix
            if len(new_text) > space:
                raise Skip          # 放不下

            new_prefix = write_uleb128(2 * len(new_text), prefix_len)
            out[p:p + prefix_len] = new_prefix
            out[start:start + space] = new_text + b"\x00" * (space - len(new_text))
            edits.append({
                "offset": start,
                "space": space,
                "old": original.decode("utf-8", "replace"),
                "new": new_text.decode("utf-8", "replace"),
            })
        except Skip:
            continue

    return bytes(out), edits


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def selftest(original: Path, reference: Path) -> int:
    want = reference.read_bytes()
    print(f"  参照  {reference}  ({len(want):,} 字节)")
    for host, port in (("127.0.0.1", 8080),):
        got, edits = patch(original.read_bytes(), host, port)
        print(f"  重放  --host {host} --port {port}  改了 {len(edits)} 处")
        if got == want:
            print("  ✅ 与参照文件逐字节一致")
            return 0
        diff = [i for i, (a, b) in enumerate(zip(got, want)) if a != b]
        print(f"  ❌ 不一致：{len(diff)} 字节不同，第一处 @ {diff[0] if diff else '-'}")
        for i in diff[:8]:
            print(f"       {i}: got {got[i]:#04x}  want {want[i]:#04x}")
        return 1
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("metadata", type=Path)
    ap.add_argument("--host", help="新主机（IP 或域名）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--apply", action="store_true", help="真的写文件（默认只报告）")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--selftest", type=Path, metavar="参照文件",
                    help="从官方原版重放参照文件，逐字节比对")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.metadata, args.selftest)

    if not args.host:
        ap.error("要么给 --host，要么给 --selftest")

    data = args.metadata.read_bytes()
    patched, edits = patch(data, args.host, args.port)
    endpoint = args.host if args.port in (80, 443) else f"{args.host}:{args.port}"

    print(f"  {args.metadata}  ({len(data):,} 字节)")
    print(f"  目标  http://{endpoint}")
    print()

    summary: dict[str, int] = {}
    for e in edits:
        summary[e["old"]] = summary.get(e["old"], 0) + 1
    for old, n in sorted(summary.items(), key=lambda x: -x[1]):
        print(f"    {n:>3} × {old}")

    print()
    print(f"  共改写 {len(edits)} 处")

    left = len(re.findall(rb"bps-pmnet\.com|conspiracyrick\.com", patched))
    if left:
        print(f"  ⚠️ 仍有 {left} 处旧域名（多数是类型名/标识符表，运行时读不到）")
    else:
        print("  ✅ 零残留")

    if len(patched) != len(data):
        print("  ❌ 文件长度变了，绝对不能这样写回去")
        return 1
    print(f"  ✅ 文件长度不变（{len(patched):,} 字节）")

    if not args.apply:
        print("\n  （试运行，未写入。加 --apply 生效）")
        return 0

    out = args.output or args.metadata.with_suffix(args.metadata.suffix + ".patched")
    out.write_bytes(patched)
    print(f"\n  已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
