#!/usr/bin/env bash
# 对着真 movieclaw 服务器跑一遍 CLI，逐条断言退出码与输出形态。
# 用法见 e2e/README.md。需要 MCLAW（二进制路径）与一个可用凭证。
set -u
M=${MCLAW:-mclaw}
SUFFIX=$$-$RANDOM
BASE=${TMPDIR:-/tmp}/mclaw-e2e-$SUFFIX
mkdir -p "$BASE/movies" "$BASE/tv" "$BASE/stdin"
pass=0; fail=0

# ck <期望退出码> <说明> <命令...>
ck() {
  local want=$1 desc=$2; shift 2
  local out; out=$("$@" 2>&1); local got=$?
  if [ "$got" = "$want" ]; then pass=$((pass+1)); printf '  ✓ %s\n' "$desc"
  else fail=$((fail+1)); printf '  ✗ %s（期望退出码 %s，实际 %s）\n%s\n' \
    "$desc" "$want" "$got" "$(echo "$out" | head -6)"; fi
}
# has <正则> <说明> <命令...>
has() {
  local re=$1 desc=$2; shift 2
  local out; out=$("$@" 2>&1)
  if grep -qE "$re" <<<"$out"; then pass=$((pass+1)); printf '  ✓ %s\n' "$desc"
  else fail=$((fail+1)); printf '  ✗ %s（输出里没有 /%s/）\n%s\n' \
    "$desc" "$re" "$(echo "$out" | head -8)"; fi
}

echo "— 只读查询（生成层）"
for c in "library list" "jobs list" "dl list" "site list" "subscriptions list" \
         "app show" "net show" "scrape show" "notices list" "rules list" \
         "llm provider show" "transcode status" "ui prefs show" "auth me" \
         "logs days" "watch list" "webhook show" "appearance show" \
         "discover region show" "members list"; do
  ck 0 "mclaw $c" $M $c -o json
done

echo "— 输出格式"
has '^\[|^\{' "-o json 输出 JSON" $M library list -o json
has '^- |^\{\}|^\[\]|: ' "-o yaml 输出 YAML" $M jobs list -o yaml
ck 0 "--quiet 不输出数据" $M library list --quiet

echo "— 写操作与请求体（三种传参形态）"
ck 0 "逐字段 + JSON 字面量标志" $M library create \
  --name "E2E电影库-$SUFFIX" --kind movie --root-paths-json "[\"$BASE/movies\"]" -o json
printf '{"name":"E2E剧集库-%s","kind":"tv","root_paths":["%s/tv"]}\n' "$SUFFIX" "$BASE" > "$BASE/body.json"
ck 0 "--input 读文件" $M library create --input "$BASE/body.json" -o json
printf '{"name":"E2E标准输入库-%s","kind":"movie","root_paths":["%s/stdin"]}\n' "$SUFFIX" "$BASE" \
  | ck 0 "--input - 读 stdin" $M library create --input - -o json

echo "— 错误路径与退出码"
ck 2 "缺少必填参数 → 2" $M library create --name 只有名字
ck 2 "路径参数类型不对 → 2" $M library get 不是数字
ck 2 "JSON 字面量标志内容非法 → 2" $M library create --name X --kind movie --root-paths-json 不是json
ck 1 "业务错误（资源不存在）→ 1" $M library get 99999
ck 3 "凭证无效 → 3" env MOVIECLAW_TOKEN=bad-token $M auth me
ck 4 "连不上服务器 → 4" $M --server http://127.0.0.1:1 library list
ck 4 "地址缺 http:// → 4" $M --server 127.0.0.1:8799 library list
ck 2 "未指定服务器 → 2" env -u MOVIECLAW_SERVER MOVIECLAW_CONFIG_DIR="$BASE/empty" $M library list
ck 5 "破坏性操作缺 --yes → 5" $M library delete 1
ck 2 "download 无搜索快照 → 2" env MOVIECLAW_CONFIG_DIR="$BASE/empty" $M download 3
ck 2 "download 行号不是整数 → 2" $M download abc
ck 2 "download 行号与 --url 并用 → 2" $M download 1 --url http://x
ck 2 "--sort 取值非法 → 2" $M search torrents 测试 --sort 不存在
ck 2 "未知命令 → 2" $M 不存在的命令

echo "— 帮助与命令面"
has '示例' "生成命令帮助含示例" $M subscriptions list --help
has 'organize-files' "精选命令并入 library 组" $M library --help
has 'torrents' "search 组含精选与生成命令" $M search --help
has '行号' "download 帮助说明两种形态" $M download --help
has 'set' "一个名字可以既是命令又是组" $M dl limits --help
ck 0 "根帮助" $M --help

echo "— 长任务与作业"
ck 0 "长任务 --wait=false 立即返回" $M library scan start 1 --wait=false -o json
ck 1 "jobs wait 任务不存在 → 1" $M jobs wait 不存在的任务 --wait-timeout 5s

echo
echo "通过 $pass 项，失败 $fail 项"
exit $((fail > 0))
