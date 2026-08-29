#!/usr/bin/env bash
# 对着 e2e/stub.py 跑 SSE 与工作流：搜索流 → 快照 → 行号下载、会话流、Job 等待。
set -u
M=${MCLAW:-mclaw}
S=${STUB_SERVER:-http://127.0.0.1:8798}
CFG=$(mktemp -d)
export MOVIECLAW_CONFIG_DIR=$CFG MOVIECLAW_TOKEN=stub-token
trap 'rm -rf "$CFG"' EXIT
run() { $M --server "$S" "$@"; }
pass=0; fail=0
ck() { local want=$1 desc=$2; shift 2
  local out; out=$(run "$@" 2>&1); local got=$?
  if [ "$got" = "$want" ]; then pass=$((pass+1)); printf '  ✓ %s\n' "$desc"
  else fail=$((fail+1)); printf '  ✗ %s（期望 %s 实际 %s）\n%s\n' "$desc" "$want" "$got" "$(echo "$out"|head -8)"; fi; }
has() { local re=$1 desc=$2; shift 2
  local out; out=$(run "$@" 2>&1)
  if grep -qE "$re" <<<"$out"; then pass=$((pass+1)); printf '  ✓ %s\n' "$desc"
  else fail=$((fail+1)); printf '  ✗ %s（缺 /%s/）\n%s\n' "$desc" "$re" "$(echo "$out"|head -10)"; fi; }

echo "— 搜索流"
has '演示站：2 条' "站点进度打到 stderr" search torrents 沙丘2
has '"row": 1' "结果带稳定行号" search torrents 沙丘2 -o json
has '坏站：失败——连接超时' "站点失败如实报告" search torrents 沙丘2
run search torrents 沙丘2 -o json > "$CFG/hits.json" 2>/dev/null
python3 - "$CFG/hits.json" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))
order = [r["seeders"] for r in rows]
print("  ✓ 默认按 seeders 降序" if order == sorted(order, reverse=True) else f"  ✗ 排序不对：{order}")
want = ["row", "title", "size", "seeders", "resolution", "group", "site", "free"]
print("  ✓ 行视图列序固定" if list(rows[0]) == want else f"  ✗ 行视图列序不对：{list(rows[0])}")
PY
has '2160p' "--resolution 过滤" search torrents 沙丘2 --resolution 2160p -o json
has '共 2 条，已截断到前 1 条' "--limit 截断并说明" search torrents 沙丘2 --limit 1
has '"event":"done"' "--stream-events 输出 NDJSON" search torrents 沙丘2 --stream-events

echo "— 行号下载与歧义消解"
ck 7 "识别歧义 → 退出码 7" download 1
has '"tmdb_id": 693134' "歧义候选走 stdout（机器可消费）" download 1
has '智能入库' "带 --tmdb-id 后完成路由" download 1 --tmdb-id 693134
has '已提交到默认下载器' "服务端 message 透传到 stderr" download 1 --tmdb-id 693134
ck 0 "--downloader-default 跳过预检" download 1 --downloader-default
ck 0 "--site-id + --url 显式形态" download --site-id demo --url https://demo.example/t/1

echo "— 会话流"
has '你好' "text_delta 拼到 stdout" session start 测试
has '工具 library.list' "工具调用打到 stderr" session start 测试
has '\[完成\] 2 步' "终态统计" session start 测试
ck 0 "agent_done → 退出码 0" session start 测试
ck 1 "agent_error → 退出码 1" session follow failing
ck 0 "agent_cancelled → 退出码 0" session follow cancelled
has '连接断开' "断流后自动续传" session follow flaky
ck 0 "续传后仍以终态收尾" session follow flaky
has 'session_id=s-1' "--detach 只返回编号" session start 测试 --detach

echo "— Job 等待"
has '处理中（50%）' "进度打到 stderr" jobs wait j-1
ck 0 "等到终态 → 退出码 0" jobs wait j-2

echo "— 不完整的流"
has '结果不完整' "done 之前断流要报不完整、不落快照" search torrents 沙丘2 --site truncated

echo
echo "通过 $pass 项，失败 $fail 项"
exit $((fail > 0))
