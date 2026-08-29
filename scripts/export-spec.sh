#!/usr/bin/env bash
# 导出 OpenAPI 基线 spec，一次写两处消费方。
#
# 两个消费方必须同版（否则模型看到的服务目录和 CLI 能跑的命令对不上）：
#   src/movieclaw_api/data/spec.json     服务端运行期读它渲染 Agent 工具描述
#   cli/internal/spec/data/spec.json     Go CLI 构建期 //go:embed 进二进制
#
# 改了路由就跑一次；漂移了 pytest 与 go test 都会红。
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
