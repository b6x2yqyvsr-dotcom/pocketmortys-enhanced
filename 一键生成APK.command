#!/bin/bash
# 用你自备的官方 APK 生成加强版客户端。双击运行。
cd "$(dirname "$0")" || exit 1
export LANG="${LANG:-zh_CN.UTF-8}"; export PYTHONUTF8=1
clear
echo "  ============================================================"
echo "   口袋莫蒂 加强版 · 一键生成"
echo "  ============================================================"
echo
echo "  需要一个官方 Pocket.Mortys.V2.41.0.apk（其它版本偏移对不上）。"
echo "  还需要完整资源清单与 AssetBundle.dat，见 README。"
echo
read -r -p "  官方 APK 的路径: " BASE
[ -f "$BASE" ] || { echo "  ❌ 找不到 $BASE"; read -r -p "  回车关闭…" _; exit 1; }
read -r -p "  服务器地址（设备能访问到的，如 192.168.1.100）: " HOST
[ -n "$HOST" ] || { echo "  ❌ 地址不能为空"; read -r -p "  回车关闭…" _; exit 1; }
PY=""; for c in python3 /opt/homebrew/bin/python3 /usr/bin/python3; do command -v "$c" >/dev/null 2>&1 && PY="$c" && break; done
[ -z "$PY" ] && { echo "  ❌ 找不到 python3"; read -r -p "  回车关闭…" _; exit 1; }
cd tools || exit 1
echo; echo "  [1/2] 生成带首启自愈层的基准包…"
"$PY" build_java_layer.py --base "$BASE" --work /tmp/pmbase \
    --manifest ../server/cdn/Aliases/rat/Android/manifest.json \
    --dat ../server/cdn/AssetBundle.dat -o /tmp/base-java.apk || {
    echo "  ❌ 第一步失败（需要 apktool / JDK / Android SDK build-tools）"; read -r -p "  回车关闭…" _; exit 1; }
echo; echo "  [2/2] 打全部补丁 + 自包含资源…"
"$PY" build_enhanced.py --base /tmp/base-java.apk --variant full \
    --metadata-host "$HOST" --out ../PocketMortys-加强版.apk || {
    echo "  ❌ 第二步失败"; read -r -p "  回车关闭…" _; exit 1; }
echo; echo "  ✅ 生成完毕：$(cd .. && pwd)/PocketMortys-加强版.apk"
read -r -p "  回车关闭…" _
