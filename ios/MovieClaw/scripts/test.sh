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
LOG="$(mktemp -t mc-test).log"

[[ -d MovieClaw.xcodeproj ]] || xcodegen generate >/dev/null

TEST_RUNNER_MC_LIVE="${MC_LIVE:-0}" TEST_RUNNER_MC_TEST_SERVER="${MC_TEST_SERVER:-}" TEST_RUNNER_MC_TEST_USERNAME="${MC_TEST_USERNAME:-}" TEST_RUNNER_MC_TEST_PASSWORD="${MC_TEST_PASSWORD:-}" xcodebuild -project MovieClaw.xcodeproj -scheme MovieClaw \
  -destination "platform=iOS Simulator,name=$SIM" -derivedDataPath "$DERIVED" \
  -clonedSourcePackagesDirPath "${MC_SPM:-$HOME/workspace/.mc-ios-spm}" "$@" test \
  >"$LOG" 2>&1 &
PID=$!

done_at=0
while kill -0 $PID 2>/dev/null; do
  if (( done_at == 0 )) && grep -qE "\*\* TEST (SUCCEEDED|FAILED)|Test Suite 'All tests' (passed|failed)|Testing cancelled" "$LOG"; then
    done_at=$SECONDS
  fi
  if (( done_at > 0 && SECONDS - done_at > 15 )); then
    kill $PID 2>/dev/null
    break
  fi
  sleep 2
done

grep -vE 'hapi|CHHapticPattern' "$LOG" | grep -E ' error:|✘|recorded an issue|XCTAssert|Test Case .*(passed|failed)|Test run with|Executed [0-9]+ test|\*\* TEST' | cut -c1-400
if grep -qE ' error:|✘|Test Case .* failed|\*\* TEST FAILED|Testing cancelled|with [1-9][0-9]* failure' "$LOG"; then
  echo "测试失败，完整日志：$LOG"
  exit 1
fi
echo "测试通过（日志：$LOG）"
