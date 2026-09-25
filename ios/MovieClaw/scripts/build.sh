#!/bin/zsh
# 编译 App（模拟器 Debug）。可选环境变量：
#   MC_SIM      模拟器名（默认 iPhone 17）；并行开发时每人用自己的模拟器
#   MC_DERIVED  DerivedData 目录（默认 ./build）
#   MC_SPM      共享的 Swift 包缓存目录（默认 ~/workspace/.mc-ios-spm，多个工作区共用省磁盘）
set -u
cd "$(dirname "$0")/.."
[[ -d MovieClaw.xcodeproj ]] || xcodegen generate >/dev/null
SIM="${MC_SIM:-iPhone 17}"
xcodebuild -project MovieClaw.xcodeproj -scheme MovieClaw \
  -destination "platform=iOS Simulator,name=$SIM" \
  -derivedDataPath "${MC_DERIVED:-build}" \
  -clonedSourcePackagesDirPath "${MC_SPM:-$HOME/workspace/.mc-ios-spm}" \
  build 2>&1 | grep -E ' error:|warning: .*(deprecated|never|unused)|BUILD (SUCCEEDED|FAILED)' | sort -u
