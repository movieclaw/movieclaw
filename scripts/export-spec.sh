#!/usr/bin/env bash
# 导出 OpenAPI 基线 spec，一次写两处消费方。
#
# 两个消费方必须同版（否则模型看到的服务目录和 CLI 能跑的命令对不上）：
#   src/movieclaw_api/data/spec.json     服务端运行期读它渲染 Agent 工具描述
#   cli/internal/spec/data/spec.json     Go CLI 构建期 //go:embed 进二进制
#
# 两份文件都是构建产物，不入 git（见 .gitignore）：镜像、发版脚本、CI 在
# 构建期现场导出；本地编译 Go CLI 或跑 go test 前跑一次本脚本；服务端
# 本地缺文件时会从代码现算，不需要跑。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi

SERVER_COPY=src/movieclaw_api/data/spec.json
CLI_COPY=cli/internal/spec/data/spec.json

mkdir -p "$(dirname "$SERVER_COPY")" "$(dirname "$CLI_COPY")"
PYTHONPATH=src "$PYTHON" -m movieclaw_api.export_openapi -o "$SERVER_COPY"
cp "$SERVER_COPY" "$CLI_COPY"
echo "已导出基线 spec：$SERVER_COPY 与 $CLI_COPY"
