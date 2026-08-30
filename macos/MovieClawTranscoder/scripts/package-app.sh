#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MovieClaw Transcoder.app"
OUTPUT_DIR="${PROJECT_DIR}/dist"
APP_DIR="${OUTPUT_DIR}/${APP_NAME}"
SIGNING_IDENTITY="${MOVIECLAW_SIGNING_IDENTITY:--}"

swift build --package-path "${PROJECT_DIR}" -c release
rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}/Contents/MacOS" "${APP_DIR}/Contents/Resources"
cp "${PROJECT_DIR}/.build/release/movieclaw-transcoder" "${APP_DIR}/Contents/MacOS/movieclaw-transcoder"
cp "${PROJECT_DIR}/Resources/Info.plist" "${APP_DIR}/Contents/Info.plist"
# App 图标：Finder、访达信息面板、⌘Tab 切换器都读它（CFBundleIconFile）
cp "${PROJECT_DIR}/Resources/AppIcon.icns" "${APP_DIR}/Contents/Resources/AppIcon.icns"

# 由内向外逐个签，不用 --deep。
#
# --deep 已被 Apple 标为不推荐：它对嵌套内容套用同一套参数，签出来的结果和
# 「各自按各自的规则签」并不等价，公证阶段常见的疑难杂症有一半出在这儿。
# 这个 bundle 只有一个可执行文件，手工排两行比 --deep 更清楚也更可控。
SIGN_ARGS=(--force --options runtime)
if [ "${SIGNING_IDENTITY}" = "-" ]; then
    # ad-hoc 签名不需要也用不了可信时间戳，别为它去连 Apple 的时间戳服务器
    SIGN_ARGS+=(--timestamp=none)
else
    # 公证要求签名带可信时间戳
    SIGN_ARGS+=(--timestamp)
fi

codesign "${SIGN_ARGS[@]}" --sign "${SIGNING_IDENTITY}" \
    "${APP_DIR}/Contents/MacOS/movieclaw-transcoder"
codesign "${SIGN_ARGS[@]}" --sign "${SIGNING_IDENTITY}" "${APP_DIR}"

echo "已生成：${APP_DIR}"

if [ "${SIGNING_IDENTITY}" = "-" ]; then
    cat >&2 <<'WARN'

⚠️  这是 ad-hoc 签名（没有 Developer ID），发行前请补上正式签名。

    ad-hoc 签名没有证书，系统只能拿二进制的 cdhash 当这个 App 的身份，
    而 cdhash **每次重新构建都会变**。后果是钥匙串：Worker 令牌那条记录的
    访问控制表认的是创建它的那个身份，换了一份构建就成了「另一个程序」，
    于是每装一次新版本都会弹窗要一次钥匙串密码（点「始终允许」只在同一份
    二进制没变时有效）。

    正式签名：
        MOVIECLAW_SIGNING_IDENTITY="Developer ID Application: 你的名字 (TEAMID)" \
            scripts/package-app.sh
    之后还需要公证（notarytool）才能在别人的机器上双击打开。
WARN
fi
