# Pocket Mortys 加强版 · 私服服务端 + 客户端补丁工具

已停服的安卓游戏 **《口袋莫蒂》Pocket Mortys** 的私服服务端，以及把官方客户端
改到能连自建服务器的补丁工具链。已完成端到端实测：干净安装 → 启动 →
自动铺好资源 → 进主菜单，**全程不需要 adb 推任何文件**。

> **本仓库不分发游戏本体资源，也不提供预编译 APK。**
> 你需要自备官方 APK —— 见下面「三步拿到能玩的 APK」。

---

## 🚀 三步拿到能玩的 APK

**第一步 · 下载工具**
到 [Releases](https://github.com/b6x2yqyvsr-dotcom/pocketmortys-enhanced/releases/latest)
下载 `pocketmortys-enhanced-tools-*.zip`，解压。

**第二步 · 准备官方 APK**

直接下载（公开来源，下载量 10 万+，2.41.0 是最后一个版本）：

**<https://github.com/Project-Pocket-Mortys/.github/releases/download/V2.41.0/Pocket.Mortys.V2.41.0.apk>**

    Pocket.Mortys.V2.41.0.apk
      174,924,654 字节
      sha256 c6efa81a0d50d8dd471cefbd9fa5b8ea3b9491b3800aa70ed97a28ecdfa98e2e

下完对一下大小和 sha256，对不上就是下坏了（或者被中间人换了），别往下走。

版本要求：Unity 2022.3.62f2 / IL2CPP metadata v31，其中
`lib/arm64-v8a/libil2cpp.so` 必须是 `52,190,824` 字节、sha256
`ff773be7d3718ee307f6342358268748e8fa2487460e7b3857715478cb8e4326`。
**其它版本偏移对不上**，脚本会在第一步就报错停下，不会打出半成品。

**第三步 · 双击生成**

| 系统 | 双击这个 |
|---|---|
| macOS | `一键生成APK.command` |
| Windows | `一键生成APK.bat` |
| Linux / 想用命令行 | `python3 tools/make_apk.py` |

它会先用中文问你两个问题（CLI 也可以直接传参）：

```bash
python3 tools/make_apk.py --base 官方.apk --host 192.168.1.100 -o 输出.apk
python3 tools/make_apk.py --check      # 只体检环境，不生成
```


```
官方 APK 的路径:  /path/to/Pocket.Mortys.V2.41.0.apk
服务器地址:       192.168.1.100      ← 设备能访问到的地址
```

约 2 分钟后得到 `PocketMortys-加强版.apk`。脚本会逐项复验（10 处 .so 补丁、
metadata 地址、明文 HTTP 放行、资源清单版本……），任何一项不过就以非 0 退出。

装到设备上（签名与官方不同，必须先卸载）：

```bash
adb uninstall com.conspiracyrick.pocketmortys
adb install -g PocketMortys-加强版.apk
```

**首次启动会慢约 1 分钟**——APK 内置的引导代码正在把 155 个资源包铺进
`files/UnityCache/`（179 MB）。别杀进程，之后每次秒进。

**前置依赖**（缺失时脚本会明确告诉你缺哪个）

| | |
|---|---|
| 通用 | JDK 17+、apktool、Android SDK 的 `build-tools` 与 `platforms` |
| macOS | `brew install apktool openjdk`，Android SDK 用 Android Studio 或 cmdline-tools |
| Windows | [apktool](https://apktool.org/) 的 `apktool.bat`、JDK、Android SDK；Python 安装时勾选 *Add python.exe to PATH* |

首次运行前可以先体检：

```bash
python3 tools/make_apk.py --check
```

---

## 它解决什么

官方 2026 年 4 月停运，三个域名 `newc137.bps-pmnet.com`、`assets.bps-pmnet.com`、
`game.bps-pmnet.com` 全部无法解析。客户端启动时会：

1. 读自己的 `files/AssetBundle.dat` 资源清单 → 空壳就报「过期资源」
2. 用不带 UA 的 `GET /` 做可达性探测 → 拿不到 200 就**永远转圈**
3. 拿 Unity 的 `Application.internetReachability` 判断有没有网 → 判「没网」就一个请求都不发
4. 去 OneTrust 拉隐私配置 → 拉不到就在 `UnityMainThreadDispatcher.Update()` 里
   每帧抛空引用，卡在「载入中……」

这个仓库把上面每一环都堵上了，并且把客户端改动做成了**可复现、自带复验**的构建脚本。

---

## 目录

```
server/            服务端（Python 3 标准库，零依赖，107+ 路由）
  run.py           入口
  pmnet/           路由、房间、战斗、SSE、JWT、SQLite、管理面板
  cdn/             资源清单、OneTrust 配置、时钟等静态响应
  data/legacy.db   从社区 PHP 版 MySQL dump 导入的静态游戏配置

tools/             客户端补丁与打包
  build_java_layer.py   首启自愈 Java 层（apktool + javac + d8）
  build_enhanced.py     全部客户端补丁 + 自包含资源 + 自动复验
  patch_official_client.py  10 处 arm64 指令补丁
  patch_metadata_exact.py   服务器地址原地等长替换（自带对拍自检）
  launch.py             一键起服务端 / 连设备 / 装包 / 启动
  repack_apk.py         保持 compression 的 APK 重打包

docs/              使用说明、踩坑记录、APK 改动清单
```

---

## 快速开始

### 1. 起服务端

```bash
cd server
PMNET_PUBLIC_HOST=192.168.1.100 python3 run.py          # 换成设备能访问到的地址
```

零依赖，只用 Python 标准库。管理面板在 `http://<地址>:8080/admin/`。

### 2. 生成客户端 APK

需要一个官方 `Pocket.Mortys.V2.41.0.apk`（Unity 2022.3.62f2 / IL2CPP metadata v31，
`libil2cpp.so` sha256 `ff773be7…c8e4326`，52,190,824 字节；其它版本偏移对不上）。

```bash
cd tools

# ① 生成带「首启自愈」Java 层的基准包
python3 build_java_layer.py \
    --base 官方.apk --work /tmp/pmbase \
    --manifest ../server/cdn/Aliases/rat/Android/manifest.json \
    --dat 完整AssetBundle.dat -o /tmp/base-java.apk

# ② 打全部补丁 + 自包含资源（会自动复验每一项）
python3 build_enhanced.py --base /tmp/base-java.apk --variant full \
    --metadata-host 192.168.1.100 --out PocketMortys-加强版.apk
```

需要 `apktool`、JDK、Android SDK build-tools（`aapt2` / `zipalign` / `apksigner`）。
资源包从社区服务端仓库取（约 90 MB，Android 组）。

---

## 客户端到底改了哪 7 处

| # | 位置 | 改什么 | 为什么 |
|---|---|---|---|
| 1 | `lib/arm64-v8a/libil2cpp.so` | 10 处 arm64 指令补丁 | 过隐私协议、**过无网络**、跳开场剧情、教程完成、多人解锁、OneTrust 死循环、多人暂停崩溃、资源失败标志 x3 |
| 2 | `assets/bin/Data/Managed/Metadata/global-metadata.dat` | 服务器域名 → 自建地址 | 原串是硬编码域名，**等长**原地替换，文件长度不变 |
| 3 | `assets/bin/Data/globalgamemanagers` | `insecureHttpOption` 0 → 2 | 否则 Unity 在发出请求前就拒掉明文 HTTP |
| 4 | `res/xml/network_security_config.xml` | 加 `base-config cleartextTrafficPermitted="true"` | 必须是**二进制 XML**，照抄文本会让游戏启动即崩 |
| 5 | `assets/AssetBundles/Android/**` | 155 个资源包 + 完整清单 | 装完即玩，不必联网下载 |
| 6 | `assets/AssetBundle.dat` | 31,826 字节完整清单 | 同上 |
| 7 | `classes4.dex` + `assets/pmseed/index.txt` | 首启自愈 | 全新安装时客户端自建的是 226 字节空壳清单，于是报「过期资源」。这段引导代码首次启动会把内置清单与 155 个包铺成客户端期望的本地形态 |

第 7 条是**最关键**的一条，细节见 `docs/使用说明.txt`。

---

## 那些踩过的坑

`docs/踩坑记录-旧版存档.txt` 里记了十几条实测结论，几条最要命的：

- metadata 的字符串表是**定长**的。改短了不补长度前缀，.NET 会把 NUL 读进 URL，
  报 `Invalid URI: Invalid port specified`；只补长度前缀不改内容，路径会被丢掉，
  客户端去请求根路径 `/`。
- 客户端本地 `AssetBundle.dat` 里存着整份清单，**启动时会和服务端的比对**。
- 客户端开机要用**不带 UA 的 `GET /`** 做可达性探测，服务端必须回 **200**。
  回 403 它判定服务器不可用，之后一个请求都不发（表现：永远转圈）。
- `resources.arsc` 必须 **STORED + 4 字节对齐**，否则 Android 12+ 直接拒绝安装。
- 重打包要**只替换指定条目**，整包 `zip -r` 会把 `resources.arsc` 压掉。

---

## 许可与声明

服务端协议形态、数据模型、房间/刷怪逻辑参考自
**Ricky Hill – Pocket Mortys Public Server**（MIT），按 MIT 要求保留署名。
本仓库为独立实现。

游戏中所有商标、美术、音频及其他素材版权归 Adult Swim / Turner Broadcasting
及原开发者所有。**本仓库不分发这些资源**，仅提供用于游戏保存的服务端与补丁工具。

与 Adult Swim、Turner Broadcasting 或原开发者无任何关联。
