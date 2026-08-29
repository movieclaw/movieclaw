#!/usr/bin/env bash
# =============================================================================
# movieclaw 应用内更新的 Release 产物构建（docs/design/in-app-update.md M1）
#
# 产出三个文件（供 GitHub Release assets，也是应用内更新的下载物）：
#   app-web.tar.gz      前端 standalone 产物（纯 JS，跨架构通用），解包即
#                       overlay 的 web/ 目录，布局与镜像内 /app/web 完全一致
#   app-backend.tar.gz  后端源码（src/ + alembic/ + alembic.ini），内含
#                       现场导出的 spec.json（运行期硬依赖，与代码严格同版）
#   manifest.json       版本、runtime 兼容要求、各文件 sha256
#
# 用法：
#   VERSION=0.2.0 ./scripts/build-release-artifacts.sh [输出目录]
#   VERSION 缺省取 pyproject.toml 的 version；输出目录缺省 dist/release
#
# 前置条件（CI 与本地相同）：
#   - pnpm install 已完成
#   - PYTHON_BIN（缺省 python3）环境已安装 pyproject 的 dependencies
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
OUT_DIR="${1:-dist/release}"
RUNTIME_VERSION="$(tr -d '[:space:]' < docker/runtime-version)"

# ---------------------------------------------------------------------------
# 版本一致性校验：VERSION（发版 tag）、pyproject、__version__ 三者必须一致，
# 否则应用内更新的「当前版本 vs 最新版本」比对就是错的。
# ---------------------------------------------------------------------------
PYPROJECT_VERSION="$("$PYTHON_BIN" -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
CODE_VERSION="$("$PYTHON_BIN" -c "
import re
text = open('src/movieclaw_api/__init__.py').read()
print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', text).group(1))
")"
VERSION="${VERSION:-$PYPROJECT_VERSION}"
if [[ "$VERSION" != "$PYPROJECT_VERSION" || "$VERSION" != "$CODE_VERSION" ]]; then
    echo "错误：版本不一致——发版版本=$VERSION，pyproject=$PYPROJECT_VERSION，__version__=$CODE_VERSION" >&2
    echo "请在发版前把三者对齐（bump pyproject.toml 与 src/movieclaw_api/__init__.py）" >&2
    exit 1
fi

echo "==> 构建 movieclaw v$VERSION 的更新产物（requires_runtime=$RUNTIME_VERSION）"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUT_DIR"

# ---------------------------------------------------------------------------
# 1) 前端：扩展 zip → next build → 按镜像内 /app/web 的布局装配
# ---------------------------------------------------------------------------
echo "==> 构建浏览器扩展与前端"
pnpm ext:publish
pnpm web:build

echo "==> 装配 web 产物"
mkdir -p "$STAGE/web"
cp -a apps/web/.next/standalone/. "$STAGE/web/"
mkdir -p "$STAGE/web/apps/web/.next"
cp -a apps/web/.next/static "$STAGE/web/apps/web/.next/static"
cp -a apps/web/public "$STAGE/web/apps/web/public"
# 与 Dockerfile 同款处理：images.unoptimized 下 sharp 永不加载，但 Next 能解析
# 到就会塞进 standalone——它是构建机架构的原生二进制，必须剔除
rm -rf "$STAGE/web/node_modules/.pnpm/@img"* "$STAGE/web/node_modules/.pnpm/sharp@"* \
    "$STAGE/web/node_modules/@img" "$STAGE/web/node_modules/sharp"

# 硬性校验：web 产物必须是纯 JS。任何残留的原生二进制（.node）都意味着
# 产物不跨架构，用户在 ARM NAS 上应用更新后会直接起不来。
NATIVE_FILES="$(find "$STAGE/web" -name '*.node' -type f)"
if [[ -n "$NATIVE_FILES" ]]; then
    echo "错误：web 产物含原生二进制，不跨架构，拒绝发布：" >&2
    echo "$NATIVE_FILES" >&2
    exit 1
fi
if [[ ! -f "$STAGE/web/apps/web/server.js" ]]; then
    echo "错误：web 产物布局异常，缺少 apps/web/server.js（entrypoint 的启动入口）" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 2) 后端：源码按仓库相对布局装配 + 现场导出 spec.json
#    相对布局（src/、alembic/、alembic.ini）是硬性约束：migrations.py 与
#    agent/prompts.py 都靠 __file__ 反推根目录（见设计文档「Agent 源码感知」）
# ---------------------------------------------------------------------------
echo "==> 装配 backend 产物"
mkdir -p "$STAGE/backend"
cp -a src "$STAGE/backend/src"
cp -a alembic "$STAGE/backend/alembic"
cp alembic.ini "$STAGE/backend/"
find "$STAGE/backend" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

# spec.json 直接导出到产物内：保证「存在」且「与产物代码严格同版」，
# 与镜像构建的现场导出（Dockerfile 阶段 5）同一思路
PYTHONPATH="$ROOT/src" "$PYTHON_BIN" -m movieclaw_api.export_openapi \
    -o "$STAGE/backend/src/movieclaw_api/data/spec.json"

for f in src/movieclaw_api/main.py src/movieclaw_api/data/spec.json alembic.ini alembic/env.py; do
    if [[ ! -s "$STAGE/backend/$f" ]]; then
        echo "错误：backend 产物布局异常，缺少 $f" >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# 3) 打包 + manifest（含 sha256，应用内更新下载后逐文件校验）
# ---------------------------------------------------------------------------
echo "==> 打包与生成 manifest"
tar -czf "$OUT_DIR/app-web.tar.gz" -C "$STAGE/web" .
tar -czf "$OUT_DIR/app-backend.tar.gz" -C "$STAGE/backend" .

VERSION="$VERSION" RUNTIME_VERSION="$RUNTIME_VERSION" OUT_DIR="$OUT_DIR" "$PYTHON_BIN" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

out = Path(os.environ["OUT_DIR"])
files = {}
for name in ("app-web.tar.gz", "app-backend.tar.gz"):
    path = out / name
    files[name] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }
manifest = {
    "schema": 1,
    "version": os.environ["VERSION"],
    "requires_runtime": int(os.environ["RUNTIME_VERSION"]),
    "files": files,
}
(out / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print((out / "manifest.json").read_text())
PY

# ---------------------------------------------------------------------------
# 4) 可选签名：配置 RELEASE_SIGNING_KEY（Ed25519 私钥 PEM 内容）后生成
#    manifest.json.sig。镜像/部署侧配置 UPDATE_MANIFEST_PUBKEY 即强制验签。
#    密钥对用 scripts/gen-release-signing-key.sh 生成。
# ---------------------------------------------------------------------------
if [[ -n "${RELEASE_SIGNING_KEY:-}" ]]; then
    OUT_DIR="$OUT_DIR" "$PYTHON_BIN" - <<'PY'
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_private_key

key = load_pem_private_key(os.environ["RELEASE_SIGNING_KEY"].encode(), password=None)
out = Path(os.environ["OUT_DIR"])
raw = (out / "manifest.json").read_bytes()
(out / "manifest.json.sig").write_bytes(base64.b64encode(key.sign(raw)) + b"\n")
print("已生成更新清单签名 manifest.json.sig")
PY
fi

echo "==> 完成，产物在 $OUT_DIR/"
ls -lh "$OUT_DIR"
