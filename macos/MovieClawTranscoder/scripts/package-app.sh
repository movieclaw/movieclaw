#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MovieClaw 转码器.app"
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

# 版本注入：发版流水线传入 tag 版本（如 0.19.0），写进 bundle 的 Info.plist。
# BuildInfo 从 CFBundleShortVersionString 读版本并在握手时上报给服务端——
# 不注入的话每个发行版都自报源文件里的占位版本，排障时无从确认用户跑的是
# 哪版 Worker。本地构建不传该变量，保留占位版本即可。改的是 bundle 里的
# 拷贝而非源文件，不弄脏工作区；必须在 codesign 之前做（签名封存 Info.plist）。
if [ -n "${MOVIECLAW_WORKER_VERSION:-}" ]; then
    plutil -replace CFBundleShortVersionString -string "${MOVIECLAW_WORKER_VERSION}" \
        "${APP_DIR}/Contents/Info.plist"
    plutil -replace CFBundleVersion -string "${MOVIECLAW_WORKER_VERSION}" \
        "${APP_DIR}/Contents/Info.plist"
    echo "已注入版本号：${MOVIECLAW_WORKER_VERSION}"
fi

# 校正二进制里记录的「链接所用 SDK 版本」（LC_BUILD_VERSION 的 sdk 字段）。
#
# SwiftPM 的新构建系统（swiftbuild，Xcode 27 起默认）链接时把部署目标 12.0 当成
# SDK 版本写进去，旧的 native 构建系统写的是真实版本（同一台机器实测：swiftbuild
# 写 12.0、native 写 27.0）。系统按这个字段判断 App「用哪版 SDK 链接」来决定新
# 行为是否生效：写成 12.0，macOS 26 起菜单、按钮、窗口的液态玻璃新外观一律不
# 启用，其它按 SDK 版本开关的行为也全退回 macOS 12 时代。代码确实是对着当前 SDK
# 编译的，这里改回真实值；同样必须在 codesign 之前（改完原签名即失效）。
BINARY="${APP_DIR}/Contents/MacOS/movieclaw-transcoder"
SDK_VERSION="$(xcrun --sdk macosx --show-sdk-version)"
BUILD_INFO="$(xcrun vtool -show-build "${BINARY}")"
MIN_OS="$(awk '$1 == "minos" { print $2; exit }' <<<"${BUILD_INFO}")"
RECORDED_SDK="$(awk '$1 == "sdk" { print $2; exit }' <<<"${BUILD_INFO}")"
if [ -n "${MIN_OS}" ] && [ -n "${SDK_VERSION}" ] && [ "${RECORDED_SDK}" != "${SDK_VERSION}" ]; then
    xcrun vtool -set-build-version macos "${MIN_OS}" "${SDK_VERSION}" \
        -replace -output "${BINARY}" "${BINARY}"
    echo "已把链接 SDK 版本从 ${RECORDED_SDK:-未知} 校正为 ${SDK_VERSION}（最低系统 ${MIN_OS} 不变）"
fi

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
