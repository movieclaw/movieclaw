#!/bin/zsh
# 跑 iOS 测试并在结果出来后收尾。
#
# 为什么要包一层：xcodebuild 跑 UI 测试时经常在「All tests passed」之后不退出
# （等模拟器里的测试宿主进程），命令行会一直挂住。这里监控日志，出现最终汇总
# 后留 15 秒余量再结束进程，并按汇总判定成败。
#
# 用法：
#   scripts/test.sh                         # 全部测试
#   scripts/test.sh -only-testing:MovieClawTests
#   MC_LIVE=1 scripts/test.sh -only-testing:MovieClawTests/LiveDecodeTests
# 可选环境变量：MC_SIM（模拟器名，默认 iPhone 17）、MC_DERIVED（DerivedData 目录，默认 build）
set -u
cd "$(dirname "$0")/.."
SIM="${MC_SIM:-iPhone 17}"
DERIVED="${MC_DERIVED:-build}"
LOG="$(mktemp -t mc-test-$(basename "$(cd ../.. && pwd)")).log"

[[ -d MovieClaw.xcodeproj ]] || xcodegen generate >/dev/null

TEST_RUNNER_MC_LIVE="${MC_LIVE:-0}" TEST_RUNNER_MC_TEST_SERVER="${MC_TEST_SERVER:-}" TEST_RUNNER_MC_TEST_USERNAME="${MC_TEST_USERNAME:-}" TEST_RUNNER_MC_TEST_PASSWORD="${MC_TEST_PASSWORD:-}" xcodebuild -project MovieClaw.xcodeproj -scheme MovieClaw \
  -destination "platform=iOS Simulator,name=$SIM" -derivedDataPath "$DERIVED" \
  -clonedSourcePackagesDirPath "${MC_SPM:-$HOME/workspace/.mc-ios-spm}" -packageAuthorizationProvider netrc "$@" test \
  >"$LOG" 2>&1 &
PID=$!

# 结束判定：优先等 xcodebuild 自己的「** TEST SUCCEEDED/FAILED **」；
# 它迟迟不出时，要求 XCTest 汇总与 Swift Testing 汇总（Test run with …）都已出现，
# 或 XCTest 汇总后 180 秒 Swift Testing 仍无动静（本次没有 Swift Testing 用例），再留 15 秒收尾。
xct_at=0
while kill -0 $PID 2>/dev/null; do
  if grep -qE "\*\* TEST (SUCCEEDED|FAILED)|Testing cancelled" "$LOG"; then
    sleep 3; kill $PID 2>/dev/null; break
  fi
  if (( xct_at == 0 )) && grep -qE "Test Suite '(All|Selected) tests' (passed|failed)" "$LOG"; then
    xct_at=$SECONDS
  fi
  if (( xct_at > 0 )); then
    if grep -qE "Test run with [0-9]+ tests? " "$LOG" && (( SECONDS - xct_at > 15 )); then
      kill $PID 2>/dev/null; break
    fi
    if (( SECONDS - xct_at > 180 )); then
      kill $PID 2>/dev/null; break
    fi
  fi
  sleep 2
done

grep -vE 'hapi|CHHapticPattern' "$LOG" | grep -E ' error:|✘|recorded an issue|XCTAssert|Test Case .*(passed|failed)|Test run with|Executed [0-9]+ test|\*\* TEST' | cut -c1-400
if grep -qE ' error:|✘|Test Case .* failed|\*\* TEST FAILED|Testing cancelled|with [1-9][0-9]* failure' "$LOG"; then
  echo "测试失败，完整日志：$LOG"
  exit 1
fi
echo "测试通过（日志：$LOG）"
