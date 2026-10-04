#!/usr/bin/env python3
"""
在不破坏 APK 结构的前提下替换文件。

为什么要这么做
--------------
targetSdk >= 30 的 APK，`resources.arsc` 必须【不压缩】且【4 字节对齐】。
用 `zip -r` 整包重压会把它压掉，安装时报：

    Failed parse during installPackageLI: Targeting R+ (version 30 and above)
    requires the resources.arsc of installed APKs to be stored uncompressed
    and aligned on a 4-byte boundary

所以这里直接读原始 APK，只替换指定条目，其余条目（含 resources.arsc 的
存储方式和所有压缩选项）原样拷贝。
"""
import argparse, shutil, sys, zipfile, os


def repack(src, dst, replacements, uncompressed_exts=('.arsc',)):
    zin = zipfile.ZipFile(src, 'r')
    names = set(zin.namelist())

    missing = [k for k in replacements if k not in names]
    if missing:
        print(f"  ⚠️ 这些条目不在原 APK 里: {missing}")

    out = zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
    replaced = []
    for item in zin.infolist():
        data = None
        if item.filename in replacements:
            data = open(replacements[item.filename], 'rb').read()
            replaced.append((item.filename, item.file_size, len(data)))
        else:
            data = zin.read(item.filename)

        # 决定是否存储
        store = (item.compress_type == zipfile.ZIP_STORED
                 or item.filename.endswith(uncompressed_exts)
                 or item.filename == 'resources.arsc')
        zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
        zi.compress_type = zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED
        zi.external_attr = item.external_attr
        zi.internal_attr = item.internal_attr
        zi.create_system = item.create_system
        out.writestr(zi, data)
    out.close()
    zin.close()
    return replaced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--replace', nargs=2, action='append', metavar=('IN_APK', 'LOCAL'),
                    required=True, help='可多次指定')
    a = ap.parse_args()
    reps = {k: v for k, v in a.replace}
    print(f"  源  {a.src}")
    print(f"  目标 {a.dst}")
    print(f"  替换 {len(reps)} 个条目")
    r = repack(a.src, a.dst, reps)
    for name, old, new in r:
        print(f"     ✓ {name:<50} {old:>12,} → {new:>12,}")
    print(f"  输出 {os.path.getsize(a.dst):,} 字节")


if __name__ == '__main__':
    main()
