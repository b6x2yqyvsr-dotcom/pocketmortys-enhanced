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

JAVA_SRC = r'''package com.pmseed;

import android.content.Context;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;

/**
 * 首启自愈 + 开机本地应答。
 *
 * 两件事：
 *  1) seed()  —— 把 APK 内置的完整清单与 155 个资源包铺成客户端期望的本地形态。
 *     全新安装时客户端自建的是 226 字节空壳清单（有版本号没清单字符串），
 *     于是报「过期资源」；只塞清单又会让它以为资源都在、用到 appdata 时才失败。
 *     两者都铺好，客户端看到的就是完全一致的本地状态。
 *
 *  2) startStub() —— 客户端开机第一件事是打一次 GET /（不带 UA 的可达性探测），
 *     拿不到 200 就**永远转圈、一个后续请求都不发**。这里在 127.0.0.1:8080
 *     起一个极小的 HTTP 服务，把开机要的那几个端点就地应答；
 *     其余请求原样转发给 PC 上的真服务端（默认 10.0.2.2:8080）。
 *
 *     地址不冲突：stub 绑的是设备内的 127.0.0.1，转发目标是宿主机 10.0.2.2，
 *     是两个不同的地址。端口被占（比如 adb reverse 已经建好隧道）时静默退出，
 *     行为退回原样。
 */
public final class SeedApplication {

    private static final String DAT = "AssetBundle.dat";
    private static final long DAT_MIN = 31826L;
    private static final int CACHE_MIN = 150;

    private static final String STUB_HOST = "127.0.0.1";
    private static final int STUB_PORT = 8080;
    private static final String BACKEND_HOST = "10.0.2.2";
    private static final int BACKEND_PORT = 8080;

    private SeedApplication() {
    }

    public static void seed(Context ctx) {
        try {
            startStub(ctx);
        } catch (Throwable t) {
            // 起不来就退回原样，不影响后面
        }
        try {
            File base = ctx.getExternalFilesDir(null);
            if (base == null) {
                return;
            }
            if (!base.exists()) {
                base.mkdirs();
            }
            seedManifest(ctx, base);
            seedCache(ctx, base);
        } catch (Throwable t) {
            t.printStackTrace();
        }
    }

    // ────────────────────────── 本地应答 ──────────────────────────

    private static void startStub(final Context ctx) {
        Thread t = new Thread(new Runnable() {
            public void run() {
                // 端口可能被别人占着 —— 最常见的是 adb reverse 隧道（adbd 先占了
                // 127.0.0.1:8080）。那种情况下请求会走隧道到 PC 上的真服务端，
                // 行为本来就正常；但隧道可能是上一次留下的，所以这里重试一会儿，
                // 拿到端口就把开机应答接管回来。
                ServerSocket ss = null;
                for (int attempt = 0; attempt < 20 && ss == null; attempt++) {
                    try {
                        ServerSocket s2 = new ServerSocket();
                        s2.setReuseAddress(true);
                        s2.bind(new InetSocketAddress(STUB_HOST, STUB_PORT));
                        ss = s2;
                    } catch (Throwable e) {
                        try {
                            Thread.sleep(2000);
                        } catch (Throwable ignored) {
                            // 继续重试
                        }
                    }
                }
                if (ss == null) {
                    return;
                }
                while (true) {
                    try {
                        final Socket c = ss.accept();
                        Thread w = new Thread(new Runnable() {
                            public void run() {
                                handle(ctx, c);
                            }
                        });
                        w.setDaemon(true);
                        w.start();
                    } catch (Throwable e) {
                        // 继续接下一个
                    }
                }
            }
        });
        t.setDaemon(true);
        t.start();
    }

    private static void handle(Context ctx, Socket c) {
        InputStream in = null;
        OutputStream out = null;
        try {
            c.setSoTimeout(20000);
            in = c.getInputStream();
            out = c.getOutputStream();

            ByteArrayOutputStream head = new ByteArrayOutputStream();
            int b;
            int nl = 0;
            while ((b = in.read()) >= 0) {
                head.write(b);
                if (b == '\n') {
                    nl++;
                    if (nl == 2) {
                        break;
                    }
                } else if (b != '\r') {
                    nl = 0;
                }
            }
            if (head.size() == 0) {
                return;
            }
            byte[] headBytes = head.toByteArray();
            String headText = new String(headBytes, "ISO-8859-1");
            String[] lines = headText.split("\r\n");
            if (lines.length == 0) {
                return;
            }
            String[] parts = lines[0].split(" ");
            if (parts.length < 2) {
                return;
            }
            String rawPath = parts[1];

            int clen = 0;
            for (int i = 1; i < lines.length; i++) {
                int p = lines[i].indexOf(':');
                if (p > 0 && lines[i].substring(0, p).trim().equalsIgnoreCase("Content-Length")) {
                    try {
                        clen = Integer.parseInt(lines[i].substring(p + 1).trim());
                    } catch (Throwable e) {
                        clen = 0;
                    }
                }
            }
            byte[] body = new byte[clen];
            int got = 0;
            while (got < clen) {
                int n = in.read(body, got, clen - got);
                if (n < 0) {
                    break;
                }
                got += n;
            }

            String path = normalize(rawPath);

            if (path.length() == 0 || path.equals("/")) {
                send(out, 200, "application/json; charset=utf-8",
                        "{\"ok\":true,\"status\":200}");
                return;
            }
            if (path.equals("/generate_204") || path.equals("/gen_204")) {
                send(out, 204, null, "");
                return;
            }
            if (path.equals("/bannersdk/v2/applicationdata")) {
                byte[] d = readAsset(ctx, "pmseed/onetrust.json");
                if (d == null) {
                    send(out, 500, "application/json; charset=utf-8", "{}");
                } else {
                    sendBytes(out, 200, "application/json; charset=utf-8", d);
                }
                return;
            }
            if (path.equals("/Status") || path.equals("/Status/rat.json")) {
                send(out, 200, "application/json; charset=utf-8", "{}");
                return;
            }
            if (path.equals("/is-gdpr")) {
                send(out, 200, "application/json; charset=utf-8",
                        "{\"countryCode\":\"US\",\"GDPR\":false,\"CCPA\":false}");
                return;
            }
            if (path.equals("/consent")) {
                send(out, 200, "application/json; charset=utf-8",
                        "{\"countryCode\":\"US\",\"GDPR\":false,\"CCPA\":false,"
                                + "\"consentRequired\":false,\"consented\":true}");
                return;
            }
            if (path.equals("/time")) {
                long ms = System.currentTimeMillis();
                long s = ms / 1000L;
                send(out, 200, "application/json; charset=utf-8",
                        "{\"utc_timestamp\":" + ms + ",\"serverTime\":" + s
                                + ",\"server_time\":" + s + ",\"time\":" + s
                                + ",\"timestamp\":" + s + "}");
                return;
            }
            if (path.endsWith("/manifest.json") || path.equals("/Aliases/rat/Android")) {
                byte[] d = readAsset(ctx, "AssetBundles/Android/manifest.json");
                if (d != null) {
                    sendBytes(out, 200, "application/json; charset=utf-8", d);
                    return;
                }
            }

            if (!forward(out, headBytes, body, got)) {
                send(out, 503, "application/json; charset=utf-8",
                        "{\"error\":\"NO_BACKEND\"}");
            }
        } catch (Throwable t) {
            // 单条连接出问题不影响别的
        } finally {
            close(in);
            close(out);
            try {
                c.close();
            } catch (Throwable t) {
                // ignore
            }
        }
    }

    /** 其余请求原样转发给 PC 上的真服务端。返回 false 表示后端不可达。 */
    private static boolean forward(OutputStream out, byte[] head, byte[] body, int bodyLen) {
        Socket up = null;
        try {
            up = new Socket();
            up.connect(new InetSocketAddress(BACKEND_HOST, BACKEND_PORT), 4000);
            up.setSoTimeout(30000);
            OutputStream uo = up.getOutputStream();
            uo.write(head);
            if (bodyLen > 0) {
                uo.write(body, 0, bodyLen);
            }
            uo.flush();
            InputStream ui = up.getInputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = ui.read(buf)) > 0) {
                out.write(buf, 0, n);
                out.flush();
            }
            return true;
        } catch (Throwable t) {
            return false;
        } finally {
            try {
                if (up != null) {
                    up.close();
                }
            } catch (Throwable t) {
                // ignore
            }
        }
    }

    private static String normalize(String p) {
        int q = p.indexOf('?');
        if (q >= 0) {
            p = p.substring(0, q);
        }
        StringBuilder sb = new StringBuilder();
        boolean prevSlash = false;
        for (int i = 0; i < p.length(); i++) {
            char ch = p.charAt(i);
            if (ch == '/') {
                if (prevSlash) {
                    continue;
                }
                prevSlash = true;
            } else {
                prevSlash = false;
            }
            sb.append(ch);
        }
        String s = sb.toString();
        while (s.length() > 1 && s.endsWith("/")) {
            s = s.substring(0, s.length() - 1);
        }
        return s;
    }

    private static void send(OutputStream out, int code, String ctype, String body)
            throws Exception {
        sendBytes(out, code, ctype, body.getBytes("UTF-8"));
    }

    private static void sendBytes(OutputStream out, int code, String ctype, byte[] body)
            throws Exception {
        StringBuilder sb = new StringBuilder();
        sb.append("HTTP/1.1 ").append(code).append(' ').append(reason(code)).append("\r\n");
        if (ctype != null) {
            sb.append("Content-Type: ").append(ctype).append("\r\n");
        }
        sb.append("Content-Length: ").append(body.length).append("\r\n");
        sb.append("Connection: close\r\n\r\n");
        out.write(sb.toString().getBytes("ISO-8859-1"));
        if (body.length > 0) {
            out.write(body);
        }
        out.flush();
    }

    private static String reason(int code) {
        if (code == 204) {
            return "No Content";
        }
        if (code == 404) {
            return "Not Found";
        }
        if (code == 500) {
            return "Internal Server Error";
        }
        if (code == 503) {
            return "Service Unavailable";
        }
        return "OK";
    }

    // ────────────────────────── 铺本地文件 ──────────────────────────

    private static void seedManifest(Context ctx, File base) {
        File dat = new File(base, DAT);
        if (dat.length() < DAT_MIN) {
            copyAsset(ctx, DAT, dat);
        }
    }

    private static void seedCache(Context ctx, File base) {
        File shared = new File(new File(base, "UnityCache"), "Shared");
        if (shared.isDirectory() && shared.list() != null
                && shared.list().length >= CACHE_MIN) {
            return;
        }
        String index = readTextAsset(ctx, "pmseed/index.txt");
        if (index == null) {
            return;
        }
        shared.mkdirs();
        for (String raw : index.split("\n")) {
            String line = raw.trim();
            if (line.length() == 0) {
                continue;
            }
            int sp = line.indexOf(' ');
            if (sp <= 0) {
                continue;
            }
            String id = line.substring(0, sp);
            String dir = line.substring(sp + 1).trim();
            File dest = new File(new File(shared, id), dir);
            File data = new File(dest, "__data");
            if (data.length() > 0) {
                continue;
            }
            dest.mkdirs();
            if (!copyAsset(ctx, "AssetBundles/Android/" + id + ".assetbundle", data)) {
                continue;
            }
            writeText(new File(dest, "__info"),
                    "-1\n" + (System.currentTimeMillis() / 1000L) + "\n1\n__data\n");
        }
    }

    private static boolean copyAsset(Context ctx, String name, File dest) {
        InputStream in = null;
        OutputStream out = null;
        try {
            in = ctx.getAssets().open(name);
            out = new FileOutputStream(dest);
            byte[] buf = new byte[262144];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            out.flush();
            return true;
        } catch (Throwable t) {
            return false;
        } finally {
            close(in);
            close(out);
        }
    }

    private static byte[] readAsset(Context ctx, String name) {
        InputStream in = null;
        try {
            in = ctx.getAssets().open(name);
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) {
                bo.write(buf, 0, n);
            }
            return bo.toByteArray();
        } catch (Throwable t) {
            return null;
        } finally {
            close(in);
        }
    }

    private static String readTextAsset(Context ctx, String name) {
        byte[] d = readAsset(ctx, name);
        if (d == null) {
            return null;
        }
        try {
            return new String(d, "UTF-8");
        } catch (Throwable t) {
            return null;
        }
    }

    private static void writeText(File f, String s) {
        OutputStream out = null;
        try {
            out = new FileOutputStream(f);
            out.write(s.getBytes("UTF-8"));
            out.flush();
        } catch (Throwable t) {
            // 下次启动再试
        } finally {
            close(out);
        }
    }

    private static void close(Object c) {
        try {
            if (c instanceof InputStream) {
                ((InputStream) c).close();
            }
        } catch (Throwable t) {
            // ignore
        }
        try {
            if (c instanceof OutputStream) {
                ((OutputStream) c).close();
            }
        } catch (Throwable t) {
            // ignore
        }
    }
}
'''

ANCHOR = '    invoke-super {p0, p1}, Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V\n'
CALL = ANCHOR + ('\n    # 首启自愈（见 com.pmseed.SeedApplication）\n'
                 '    invoke-static {p0}, Lcom/pmseed/SeedApplication;->seed(Landroid/content/Context;)V\n')



def run(argv, **kw):
    """调用外部程序。Windows 上 .bat/.cmd 必须经 cmd /c，否则 CreateProcess 起不来。"""
    argv = [str(a) for a in argv]
    if os.name == "nt" and argv and argv[0].lower().endswith((".bat", ".cmd")):
        argv = ["cmd", "/c", *argv]
    return subprocess.run(argv, **kw)


def tool(bt, name):
    """兼容 Windows 的 .exe / .bat 后缀。"""
    import pathlib as _p
    bt = _p.Path(bt)
    for suffix in (".exe", ".bat", ".cmd", ""):
        cand = bt / (name + suffix)
        if cand.is_file():
            return str(cand)
    return str(bt / name)


def sdk_paths():
    sdk = pathlib.Path(os.environ.get("ANDROID_HOME") or
                       pathlib.Path.home() / "Library" / "Android" / "sdk")
    bt = sorted((sdk / "build-tools").glob("*"))[-1]
    jar = sorted((sdk / "platforms").glob("android-*/android.jar"))[-1]
    return bt, jar


APKTOOL = shutil.which("apktool") or shutil.which("apktool.bat") or "apktool"


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
        run([APKTOOL, "d", "-f", "-o", str(a.work), str(a.base)], check=True)

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
        run([javac, "--release", "11", "-nowarn", "-cp", str(android_jar),
                        "-d", str(td / "cls"),
                        str(td / "src/com/pmseed/SeedApplication.java")], check=True)
        run([tool(bt, "d8"), "--min-api", "28", "--lib", str(android_jar),
                        "--output", str(td / "dex"),
                        str(td / "cls/com/pmseed/SeedApplication.class")], check=True)
        dex_out = a.out.parent / "classes4.dex"
        dex_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(td / "dex/classes.dex", dex_out)
        print(f"  已生成 {dex_out}")

    # 4. 打包（只为了拿到编好的 AndroidManifest.xml）
    run([APKTOOL, "b", str(a.work), "-o", str(a.out)], check=True)
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
    print("\n  注意：目标地址建议用 127.0.0.1 —— APK 里内置的开机应答会绑")
    print("        127.0.0.1:8080 本地回答可达性探测，其余请求转发给 10.0.2.2:8080。")
    print("        用这个包时不要再建 adb reverse（会占住同一个端口）。")
    print("\n  下一步：")
    print(f"    python3 build_enhanced.py --base {a.out} --variant full \\")
    print(f"        --metadata-host 10.0.2.2 --out 输出.apk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
