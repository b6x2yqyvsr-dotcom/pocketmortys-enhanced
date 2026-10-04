#!/usr/bin/env python3
"""重建「首启自愈」Java 层，并把它注入 APK。

为什么需要
----------
游戏启动必须读 files/AssetBundle.dat。全新安装时它自己建的是 226 字节空壳
（有版本号、没有清单字符串），于是判「过期资源」并弹「无法连接」。
光把完整清单塞进去也不行：客户端会认为资源都在，不再读 APK 里的包，
真用到 appdata 时才失败。所以两者都要铺。

做法
----
1. 用 apktool 解包官方 APK，在 UnityPlayerActivity.onCreate 里插一行
   invoke-static 调用 SeedApplication.seed()；
2. SeedApplication（Java 编译成 classes4.dex）首启把 APK 内置的
   AssetBundle.dat 与 155 个资源包铺成客户端期望的本地形态；
3. 再用 tools/build_enhanced.py 打全部客户端补丁并自包含资源。

只用「缺失」或「比内置小」时才写，绝不覆盖游戏自己写好的有效文件。

用法
----
    python3 build_java_layer.py --base 官方.apk --work /tmp/pmbase \\
        --manifest ../2-服务端/cdn/Aliases/rat/Android/manifest.json \\
        --dat AssetBundle.dat -o out/base-java.apk
"""
import argparse, os, pathlib, shutil, struct, subprocess, sys, tempfile

JAVA_SRC = r'''
package com.pmseed;
import android.content.Context;
import java.io.*;
public final class SeedApplication {
    private static final String DAT = "AssetBundle.dat";
    private static final long DAT_MIN = 31826L;
    private static final int CACHE_MIN = 150;
    private SeedApplication() {}
    public static void seed(Context ctx) {
        try {
            File base = ctx.getExternalFilesDir(null);
            if (base == null) return;
            if (!base.exists()) base.mkdirs();
            File dat = new File(base, DAT);
            if (dat.length() < DAT_MIN) copyAsset(ctx, DAT, dat);
            seedCache(ctx, base);
        } catch (Throwable t) { t.printStackTrace(); }
    }
    private static void seedCache(Context ctx, File base) {
        File shared = new File(new File(base, "UnityCache"), "Shared");
        if (shared.isDirectory() && shared.list() != null
                && shared.list().length >= CACHE_MIN) return;
        String index = readTextAsset(ctx, "pmseed/index.txt");
        if (index == null) return;
        shared.mkdirs();
        for (String raw : index.split("\n")) {
            String line = raw.trim();
            if (line.length() == 0) continue;
            int sp = line.indexOf(' ');
            if (sp <= 0) continue;
            String id = line.substring(0, sp);
            String dir = line.substring(sp + 1).trim();
            File dest = new File(new File(shared, id), dir);
            File data = new File(dest, "__data");
            if (data.length() > 0) continue;
            dest.mkdirs();
            if (!copyAsset(ctx, "AssetBundles/Android/" + id + ".assetbundle", data)) continue;
            writeText(new File(dest, "__info"),
                    "-1\n" + (System.currentTimeMillis() / 1000L) + "\n1\n__data\n");
        }
    }
    private static boolean copyAsset(Context ctx, String name, File dest) {
        InputStream in = null; OutputStream out = null;
        try {
            in = ctx.getAssets().open(name);
            out = new FileOutputStream(dest);
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            out.flush();
            return true;
        } catch (Throwable t) { return false; }
        finally { close(in); close(out); }
    }
    private static String readTextAsset(Context ctx, String name) {
        InputStream in = null;
        try {
            in = ctx.getAssets().open(name);
            BufferedReader r = new BufferedReader(new InputStreamReader(in, "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) sb.append(line).append('\n');
            return sb.toString();
        } catch (Throwable t) { return null; }
        finally { close(in); }
    }
    private static void writeText(File f, String s) {
        OutputStream out = null;
        try { out = new FileOutputStream(f); out.write(s.getBytes("UTF-8")); out.flush(); }
        catch (Throwable t) {}
        finally { close(out); }
    }
    private static void close(Object c) {
        try { if (c instanceof InputStream) ((InputStream) c).close(); } catch (Throwable t) {}
        try { if (c instanceof OutputStream) ((OutputStream) c).close(); } catch (Throwable t) {}
    }
}
'''

ANCHOR = '    invoke-super {p0, p1}, Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V\n'
CALL = ANCHOR + ('\n    # 首启自愈（见 com.pmseed.SeedApplication）\n'
                 '    invoke-static {p0}, Lcom/pmseed/SeedApplication;->seed(Landroid/content/Context;)V\n')


def sdk_paths():
    sdk = pathlib.Path(os.environ.get("ANDROID_HOME") or
                       pathlib.Path.home() / "Library" / "Android" / "sdk")
    bt = sorted((sdk / "build-tools").glob("*"))[-1]
    jar = sorted((sdk / "platforms").glob("android-*/android.jar"))[-1]
    return bt, jar


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=pathlib.Path, required=True)
    ap.add_argument("--work", type=pathlib.Path, required=True)
    ap.add_argument("--manifest", type=pathlib.Path, required=True,
                    help="完整资源清单 manifest.json（用来生成 UnityCache 目录名）")
    ap.add_argument("--dat", type=pathlib.Path, required=True, help="完整 AssetBundle.dat")
    ap.add_argument("-o", "--out", type=pathlib.Path, required=True)
    a = ap.parse_args()

    bt, android_jar = sdk_paths()
    javac = shutil.which("javac") or "/opt/homebrew/opt/openjdk/bin/javac"

    # 1. 折包
    if not (a.work / "AndroidManifest.xml").is_file():
        subprocess.run(["apktool", "d", "-f", "-o", str(a.work), str(a.base)], check=True)

    # 2. 插入调用
    target = a.work / "smali_classes2/com/unity3d/player/UnityPlayerActivity.smali"
    if not target.is_file():
        hits = list(a.work.glob("smali*/com/unity3d/player/UnityPlayerActivity.smali"))
        if not hits:
            sys.exit("解包目录里找不到 UnityPlayerActivity.smali")
        target = hits[0]
    src = target.read_text()
    if "com/pmseed/SeedApplication" not in src:
        if src.count(ANCHOR) != 1:
            sys.exit(f"onCreate 里锚点出现 {src.count(ANCHOR)} 次，无法安全插入")
        target.write_text(src.replace(ANCHOR, CALL, 1))
        print(f"  已注入 onCreate: {target}")

    # 3. 编译 Java -> dex
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "src/com/pmseed").mkdir(parents=True)
        (td / "src/com/pmseed/SeedApplication.java").write_text(JAVA_SRC, encoding="utf-8")
        subprocess.run([javac, "--release", "11", "-nowarn", "-cp", str(android_jar),
                        "-d", str(td / "cls"),
                        str(td / "src/com/pmseed/SeedApplication.java")], check=True)
        subprocess.run([str(bt / "d8"), "--min-api", "28", "--lib", str(android_jar),
                        "--output", str(td / "dex"),
                        str(td / "cls/com/pmseed/SeedApplication.class")], check=True)
        dex_out = a.out.parent / "classes4.dex"
        dex_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(td / "dex/classes.dex", dex_out)
        print(f"  已生成 {dex_out}")

    # 4. 打包（只为了拿到编好的 AndroidManifest.xml）
    subprocess.run(["apktool", "b", str(a.work), "-o", str(a.out)], check=True)
    print(f"  已生成 {a.out}")

    # 5. 生成 index.txt
    import json
    mani = json.loads(a.manifest.read_text(encoding="utf-8"))
    lines = []
    for k, v in mani.items():
        if isinstance(v, dict):
            lines.append(f"{k} {'0' * 24}{struct.pack('<I', int(v.get('version', 1)) & 0xFFFFFFFF).hex()}")
    (a.out.parent / "index.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  已生成 index.txt（{len(lines)} 条）")
    print("\n  下一步：")
    print(f"    python3 build_enhanced.py --base {a.out} --variant full \\")
    print(f"        --metadata-host 10.0.2.2 --out 输出.apk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
