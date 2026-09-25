#!/bin/zsh
# 把最近一次编译的 App 装到模拟器，直达某个站内路由并截图。
#   scripts/shot.sh /library/1 /tmp/lib.png [等待秒数，默认 6]
# 与网页同一路由对照：node /tmp/mc-shots/shoot.mjs <目录> /library/1
# 登录信息默认本机 dev 环境，可用 MC_SERVER / MC_USER / MC_PASS 覆盖。
set -u
cd "$(dirname "$0")/.."
ROUTE="$1"; OUT="$2"; WAIT="${3:-6}"
SIM="${MC_SIM:-iPhone 17}"
APP="${MC_DERIVED:-build}/Build/Products/Debug-iphonesimulator/MovieClaw.app"
xcrun simctl boot "$SIM" 2>/dev/null; xcrun simctl bootstatus "$SIM" -b >/dev/null
xcrun simctl install "$SIM" "$APP"
xcrun simctl terminate "$SIM" com.movieclaw.app 2>/dev/null
xcrun simctl launch "$SIM" com.movieclaw.app \
  -mcServer "${MC_SERVER:-http://localhost:3000}" -mcUser "${MC_USER:-admin}" -mcPass "${MC_PASS:-mclaw-dev-2026}" \
  -mcRoute "$ROUTE" >/dev/null
sleep "$WAIT"
xcrun simctl io "$SIM" screenshot "$OUT" >/dev/null 2>&1 && echo "截图：$OUT"
