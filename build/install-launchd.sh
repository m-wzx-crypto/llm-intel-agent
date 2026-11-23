#!/bin/bash
# launchd 早报/晚报/周报任务：install / remove / status / run
set -uo pipefail
BUILD_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_BIN="/Applications/LLMIntelAgent.app/Contents/MacOS/LLMIntelAgent"
LABELS=(com.llmintel.agent.morning com.llmintel.agent.evening com.llmintel.agent.weekly)

case "${1:-status}" in
  install)
    [ -x "$APP_BIN" ] || { echo "⚠️ 先把 dist/LLMIntelAgent.app 拖到「应用程序」"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents"
    for L in "${LABELS[@]}"; do
      cp "$BUILD_DIR/$L.plist" "$HOME/Library/LaunchAgents/$L.plist"
      launchctl bootout "gui/$UID/$L" 2>/dev/null
      launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/$L.plist" 2>/dev/null \
        || launchctl load -w "$HOME/Library/LaunchAgents/$L.plist"
      echo "  ✅ $L"
    done
    echo "已注册：早报 08:00（必跑）· 晚报 20:00（token 有余量才跑）· 周报 周一 09:00"
    ;;
  remove)
    # 旧版每天 07:30 的 daily 任务已被早报/晚报取代，一并清掉，避免一天跑两遍
    for L in "${LABELS[@]}" com.llmintel.agent.daily; do
      launchctl bootout "gui/$UID/$L" 2>/dev/null
      rm -f "$HOME/Library/LaunchAgents/$L.plist"
      echo "  已注销 $L"
    done
    ;;
  run)
    # run morning | run evening
    L="com.llmintel.agent.${2:-morning}"
    launchctl kickstart -k "gui/$UID/$L" 2>/dev/null && echo "已触发一次 $L"
    sleep 3; tail -6 "/tmp/llmintel.${2:-morning}.out" 2>/dev/null
    ;;
  status|*)
    for L in "${LABELS[@]}"; do
      if launchctl print "gui/$UID/$L" >/dev/null 2>&1; then
        echo "$L: 已注册"
        launchctl print "gui/$UID/$L" 2>/dev/null | grep -E "state =|runs =|last exit code" | sed 's/^/   /'
      else
        echo "$L: 未注册"
      fi
      tail -3 "/tmp/llmintel.${L##*.}.out" 2>/dev/null | sed 's/^/   日志 /'
    done
    ;;
esac
