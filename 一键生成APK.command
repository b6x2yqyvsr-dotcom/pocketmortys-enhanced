#!/bin/bash
# macOS 双击运行。真正的逻辑在 tools/make_apk.py 里，这里只是个壳。
cd "$(dirname "$0")" || exit 1
export LANG="${LANG:-zh_CN.UTF-8}"; export PYTHONUTF8=1
clear
PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    command -v "$c" >/dev/null 2>&1 && PY="$c" && break
done
if [ -z "$PY" ]; then
    echo "  ❌ 找不到 python3。先跑：  xcode-select --install"
    read -r -p "  回车关闭…" _; exit 1
fi
"$PY" tools/make_apk.py "$@"
rc=$?
if [ $rc -ne 0 ]; then
    echo
    read -r -p "  出错了，按回车关闭…" _
fi
