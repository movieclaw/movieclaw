#!/bin/zsh
# 构建打了 MovieClaw 补丁的 libmpv（Libmpv.xcframework，LGPL），放进 Vendor/MPVKit。
#
# 构建产物（约 9MB，含真机 arm64 与模拟器 arm64/x86_64）直接入库，拉下代码即可编译 App，平时不用跑本脚本。
# 只有改了 patches/ 下的补丁、或升级 mpv / MPVKit 版本时才重跑，跑完把新的 Libmpv.xcframework 一并提交。
#
# 为什么要自己构建：上游 MPVKit 的 moltenvk 渲染上下文只在视频参数变化时读取 Metal 渲染面尺寸，
# 旋转屏幕后 mpv 仍按旧尺寸排布画面，只能由 App 销毁重建视频输出（真机约 0.6~1.2 秒黑屏）。
# Vendor/MPVKit/patches/ 下的两个补丁：
# - 0004-moltenvk-detect-resize：VO 线程每轮核对渲染面尺寸，变了立即调整交换链并重排画面；
# - 0005-vo-gpu-next-follow-swapchain-size：每帧以交换链图像的实际尺寸为准排布画面——MoltenVK 会抢在
#   mpv 之前按新尺寸重建交换链，不补这条的话旋转开头一两帧画面会缩在一角。
# 其余依赖（FFmpeg 除外）上游构建脚本直接下载预编译包，FFmpeg 与 mpv 从源码编译。
#
# 用法：
#   scripts/build-libmpv.sh [工作目录，默认 ~/workspace/mpvkit-build]
#   MC_LIBMPV_ONLY=1 scripts/build-libmpv.sh   只重编 libmpv（依赖已编好、只改了 mpv 补丁时用，几分钟）
# 耗时：M1 上首次约数十分钟（主要是 FFmpeg 三个架构），需要约 10GB 临时磁盘；Homebrew 的 meson/ninja
# 由上游脚本按需自动安装。只构建 iOS 真机与模拟器（arm64 + 模拟器 x86_64）。
set -euo pipefail
cd "$(dirname "$0")/.."
VENDOR="$PWD/Vendor/MPVKit"
WORK="${1:-$HOME/workspace/mpvkit-build}"
MPVKIT_TAG="1.0.0"

if [[ ! -d "$WORK/.git" ]]; then
  echo "克隆 MPVKit $MPVKIT_TAG → $WORK"
  git clone --depth 1 --branch "$MPVKIT_TAG" https://github.com/mpvkit/MPVKit "$WORK"
fi

cd "$WORK"
# 上游脚本用 wget 下载预编译依赖；Homebrew 的 wget 经常因依赖库升级而失效，统一换成系统自带 curl
sed -i '' 's|try! Utility.launch(path: "wget", arguments: \["-O", outputFileName, library.url\], currentDirectoryURL: directoryURL)|try! Utility.launch(path: "/usr/bin/curl", arguments: ["-fL", "--retry", "3", "-o", outputFileName, library.url], currentDirectoryURL: directoryURL)|' \
  Sources/BuildScripts/XCFrameworkBuild/base.swift
sed -i '' 's|try! Utility.launch(path: "wget", arguments: \["-q", "-O", tmpChecksum.path, target.checksum\], currentDirectoryURL: FileManager.default.temporaryDirectory)|try! Utility.launch(path: "/usr/bin/curl", arguments: ["-fsL", "-o", tmpChecksum.path, target.checksum], currentDirectoryURL: FileManager.default.temporaryDirectory)|' \
  Sources/BuildScripts/XCFrameworkBuild/base.swift

# 补丁按文件名顺序在上游 0001~0003 之后应用
cp "$VENDOR"/patches/*.patch Sources/BuildScripts/patch/libmpv/
# 上游脚本只在首次克隆 mpv 源码时打补丁；源码已在时补上新增的补丁（已打过的跳过）
MPV_SRC="$(find "$WORK/dist" -maxdepth 1 -type d -name 'libmpv-v*' 2>/dev/null | head -1 || true)"
if [[ -n "$MPV_SRC" ]]; then
  for patch in "$VENDOR"/patches/*.patch; do
    git -C "$MPV_SRC" apply --reverse --check "$patch" 2>/dev/null && continue
    echo "补打 $(basename "$patch")"
    git -C "$MPV_SRC" apply "$patch"
  done
fi

# 只重编 libmpv：在上游构建入口里加一个环境变量开关（只加一次）
if ! grep -q MC_LIBMPV_ONLY Sources/BuildScripts/XCFrameworkBuild/main.swift; then
  sed -i '' 's|^    try Build.performCommand(options)$|    try Build.performCommand(options)\
    if ProcessInfo.processInfo.environment["MC_LIBMPV_ONLY"] == "1" { try BuildMPV().buildALL(); exit(0) }|' \
    Sources/BuildScripts/XCFrameworkBuild/main.swift
fi

make build platform=ios,isimulator

OUT="$(find "$WORK" -path '*release/xcframework/Libmpv.xcframework' -maxdepth 5 -type d | head -1)"
if [[ -z "$OUT" ]]; then
  echo "构建结束但没找到 Libmpv.xcframework，请查看上面的输出" >&2
  exit 1
fi
rm -rf "$VENDOR/Libmpv.xcframework"
cp -R "$OUT" "$VENDOR/Libmpv.xcframework"
echo "已更新：$VENDOR/Libmpv.xcframework"
