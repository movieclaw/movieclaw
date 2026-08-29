"""OpenAPI spec 离线导出（CLI 内置基线 spec 的唯一产出口）。

设计背景（docs/design/cli.md §2.1）：CLI 的命令树、参数、help 全部由
OpenAPI spec 动态生成。spec 有两个消费方，都从这里拿：Go CLI 在构建期把它
嵌进二进制（cli/internal/spec），服务端运行期读它渲染 Agent 的工具描述
（movieclaw_api/services/spec_catalog.py）。

    python -m movieclaw_api.export_openapi -o src/movieclaw_api/data/spec.json

要点：
- 不需要启动服务器：FastAPI 的 app.openapi() 纯内存构建；
- 输出做了确定性处理（键排序），同一份代码导出的字节完全一致，
  spec_hash() 因此可作为「CLI 基线与服务器是否同版」的偏斜检测指纹；
- tests/api/test_spec_baseline.py 有守护测试保证仓库里的基线文件与当前
  代码导出一致，改了路由忘了重新导出会直接测试失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def build_spec() -> dict[str, Any]:
    """构建当前代码的完整 OpenAPI spec（与线上 /docs 看到的一致）。"""
    # 延迟导入：让 spec_hash 等纯函数可以被轻量引用，不强制拉起整个 app 依赖
    from movieclaw_api.app import create_app

    return create_app().openapi()


def dump_spec(spec: dict[str, Any]) -> str:
    """确定性序列化：键排序 + 紧凑分隔符，保证同代码同字节。"""
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def spec_hash(spec: dict[str, Any]) -> str:
    """spec 内容指纹（sha256 前 16 位十六进制）。

    未来 /health 响应会携带该值，CLI 用它与内置基线比对做版本偏斜检测。
    """
    return hashlib.sha256(dump_spec(spec).encode("utf-8")).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 movieclaw 的 OpenAPI spec")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出文件路径；缺省打印到标准输出",
    )
    args = parser.parse_args(argv)

    spec = build_spec()
    text = dump_spec(spec)
    if args.output is None:
        print(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"已导出 OpenAPI spec：{args.output}（hash={spec_hash(spec)}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
