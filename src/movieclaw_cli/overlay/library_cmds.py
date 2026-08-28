"""精选命令：媒体库需确认操作的「预览→确认→执行」工作流。

页面上这是一个四段式对话框；CLI 把它固化为一条命令：任何正式执行都
**强制先预览并回显影响面**（改名 N 项 / 跳过 M 项），再要求 --yes。
--dry-run 只看计划不动磁盘。

本模块的命令整体覆盖生成层对应接口（同名让位机制，docs/design/cli.md §7）。
"""

from __future__ import annotations

import sys

import click

from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.output import emit
from movieclaw_cli.gen.tree_builder import wait_long_task
from movieclaw_cli.overlay.groups import output_option

# 进度轮询的端点描述（与生成层 library.scan 使用同一个 wait 实现）
_PROGRESS_OPS = {
    "library.get": {
        "operation_id": "library.get",
        "path": "/api/v1/libraries/{library_id}",
        "params": [{"name": "library_id", "in": "path"}],
    }
}


@click.command(
    name="organize-files",
    short_help="按配置的命名模板整理存量文件名（预览影响面 → --yes 确认 → 执行并等待）",
)
@click.argument("library_id", type=int)
@click.option("--dry-run", is_flag=True, help="只输出整理计划，不动磁盘")
@click.option("--yes", is_flag=True, help="确认执行（正式整理必须显式给出）")
@click.option("--wait/--no-wait", default=True, show_default=True, help="等待整理完成")
@click.option(
    "--wait-timeout", type=float, default=3600.0, show_default=True, help="--wait 的最长等待秒数"
)
@output_option
@click.pass_obj
def library_organize_files(
    settings,
    output_override: str | None,
    library_id: int,
    dry_run: bool,
    yes: bool,
    wait: bool,
    wait_timeout: float,
):
    """按当前生效的命名模板批量改名归位库内文件（默认模板即 Emby/Plex 规范）。

    模板在 ``mclaw scrape set`` 里配（也可按库覆盖）。**改了模板就跑一次
    这个命令**，存量文件才会跟着变——可以反复改、反复整理，条目目录改名时
    海报/NFO/字幕/分集剧照都跟着搬，不会留下空壳目录。

    示例：

        mclaw library organize-files 1 --dry-run     # 只看计划

        mclaw library organize-files 1 --yes         # 执行（先回显影响面）

    源文件绝不删除；执行与扫描互斥（扫描进行中会被服务端拒绝）。
    """
    api = settings.make_api()
    try:
        preview = (
            api.request("POST", f"/libraries/{library_id}/file-organization-preview") or {}
        )
        renames = preview.get("renames") or []
        skips = preview.get("skips") or []
        print(
            f"整理计划：共 {preview.get('total')} 个文件——"
            f"改名 {len(renames)} 项，已规范 {preview.get('already_ok')} 项，"
            f"跳过 {len(skips)} 项",
            file=sys.stderr,
        )
        if dry_run:
            emit(preview, output=output_override or settings.output, quiet=settings.quiet)
            return
        if not renames:
            print("没有需要整理的文件", file=sys.stderr)
            return
        if not (yes or settings.yes):
            emit(preview, output=output_override or settings.output, quiet=settings.quiet)
            raise CliError(
                f"即将改名 {len(renames)} 个文件，需要确认",
                exit_code=ExitCode.NEED_CONFIRM,
                hint="核对上面的整理计划后重跑并加 --yes；只看计划用 --dry-run",
            )
        started = api.request("POST", f"/libraries/{library_id}/file-organizations")
        if api.last_message:
            print(api.last_message, file=sys.stderr)
        emit(started, output=output_override or settings.output, quiet=settings.quiet)
        if wait:
            op = {
                "long_task": {
                    "progress_op": "library.get",
                    "progress_field": "organize_progress",
                }
            }
            wait_long_task(op, _PROGRESS_OPS, api, {"library_id": library_id}, wait_timeout)
    finally:
        api.close()


@click.command(
    name="reconcile-paths",
    short_help="修复旧根路径遗留台账（预览影响面 → --yes 执行）",
)
@click.argument("library_id", type=int)
@click.option("--old-root", required=True, help="已移除的旧根路径")
@click.option("--new-root", required=True, help="当前媒体库配置中的目标根路径")
@click.option("--dry-run", is_flag=True, help="只输出修复预览，不扫描、不修改台账")
@click.option("--yes", is_flag=True, help="确认执行修复（默认只预览）")
@output_option
@click.pass_obj
def library_reconcile_paths(
    settings,
    output_override: str | None,
    library_id: int,
    old_root: str,
    new_root: str,
    dry_run: bool,
    yes: bool,
):
    """收口容器挂载前缀变更后遗留的旧路径台账。

    正式执行会以持久化扫描作业重新盘点目标根；只会合并、标记或删除
    ``library_file`` 台账记录，绝不会删除任何媒体文件。
    """
    api = settings.make_api()
    payload = {"old_root": old_root, "new_root": new_root}
    try:
        preview = (
            api.request(
                "POST",
                f"/libraries/{library_id}/path-reconciliation-preview",
                json_body=payload,
            )
            or {}
        )
        print(
            "路径迁移预览："
            f"同相对路径 {preview.get('same_path_candidates', 0)}，"
            f"可安全合并 {preview.get('safe_merges', 0)}，"
            f"将标记缺失 {preview.get('marked_missing', 0)}，"
            f"身份冲突 {len(preview.get('conflicts') or [])}，"
            "磁盘删除 0",
            file=sys.stderr,
        )
        if dry_run:
            emit(preview, output=output_override or settings.output, quiet=settings.quiet)
            return
        if not (yes or settings.yes):
            emit(preview, output=output_override or settings.output, quiet=settings.quiet)
            raise CliError(
                "路径迁移修复会修改旧路径台账，需要确认",
                exit_code=ExitCode.NEED_CONFIRM,
                hint="核对预览后重跑并加 --yes；只看预览用 --dry-run",
            )
        started = api.request(
            "POST",
            f"/libraries/{library_id}/path-reconciliations",
            json_body=payload,
        )
        if api.last_message:
            print(api.last_message, file=sys.stderr)
        emit(started, output=output_override or settings.output, quiet=settings.quiet)
    finally:
        api.close()
