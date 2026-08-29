package overlay

import (
	"strconv"
	"time"

	"github.com/spf13/cobra"
	"github.com/yipengfei329/movieclaw/cli/internal/clierr"
	"github.com/yipengfei329/movieclaw/cli/internal/flagx"
	"github.com/yipengfei329/movieclaw/cli/internal/jsonval"
	"github.com/yipengfei329/movieclaw/cli/internal/output"
	"github.com/yipengfei329/movieclaw/cli/internal/wait"
)

// NewLibraryGroup 构造 `mclaw library` 组；生成层随后并入其余媒体库命令。
//
// 组里这两条命令各自编排「预览 → 明确确认 → 执行」，隐藏底层工作流端点：
// 页面上那是一个四段式对话框，CLI 把它固化成一条命令——任何正式执行都
// **强制先预览并回显影响面**，再要求 --yes。
func NewLibraryGroup() *cobra.Command {
	group := &cobra.Command{
		Use: "library",
		Short: "管理本地电影/剧集媒体库、库存文件、识别结果、元数据、图片与字幕；" +
			"organize-files 按 scrape 配的命名模板批量整理存量文件名",
		RunE:         func(cmd *cobra.Command, _ []string) error { return cmd.Help() },
		SilenceUsage: true,
	}
	group.Long = group.Short
	group.AddCommand(newLibraryOrganizeFilesCommand())
	group.AddCommand(newLibraryReconcilePathsCommand())
	return group
}

func newLibraryOrganizeFilesCommand() *cobra.Command {
	var dryRun, waitDone bool
	var waitTimeout time.Duration
	cmd := &cobra.Command{
		Use:   "organize-files <library_id>",
		Short: "按配置的命名模板整理存量文件名（预览影响面 → --yes 确认 → 执行并等待）",
		Long: `按当前生效的命名模板批量改名归位库内文件（默认模板即 Emby/Plex 规范）。

模板在 mclaw scrape set 里配（也可按库覆盖）。**改了模板就跑一次这个命令**，
存量文件才会跟着变——可以反复改、反复整理，条目目录改名时海报/NFO/字幕/
分集剧照都跟着搬，不会留下空壳目录。

示例：

    mclaw library organize-files 1 --dry-run     # 只看计划

    mclaw library organize-files 1 --yes         # 执行（先回显影响面）

源文件绝不删除；执行与扫描互斥（扫描进行中会被服务端拒绝）。`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "只输出整理计划，不动磁盘")
	cmd.Flags().BoolVar(&waitDone, "wait", true, "等待整理完成")
	flagx.Var(cmd.Flags(), &waitTimeout, "wait-timeout", time.Hour, "--wait 的最长等待秒数")

	return withOverrides(cmd, []string{"dry-run", "wait", "wait-timeout"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			libraryID, err := parseLibraryID(args[0])
			if err != nil {
				return err
			}
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			base := "/libraries/" + libraryID
			raw, err := client.Request("POST", base+"/file-organization-preview", nil, nil)
			if err != nil {
				return err
			}
			preview := jsonval.Object(raw)
			renames := jsonval.Array(preview.Get("renames"))
			output.Info("整理计划：共 %s 个文件——改名 %d 项，已规范 %s 项，跳过 %d 项",
				jsonval.Plain(preview.Get("total")), len(renames),
				jsonval.Plain(preview.Get("already_ok")), len(jsonval.Array(preview.Get("skips"))))
			if dryRun {
				return output.Emit(preview, s.Output, s.Quiet)
			}
			if len(renames) == 0 {
				output.Info("没有需要整理的文件")
				return nil
			}
			if !s.Yes {
				if err := output.Emit(preview, s.Output, s.Quiet); err != nil {
					return err
				}
				return clierr.Newf(clierr.NeedConfirm, "即将改名 %d 个文件，需要确认", len(renames)).
					WithHint("核对上面的整理计划后重跑并加 --yes；只看计划用 --dry-run")
			}
			started, err := client.Request("POST", base+"/file-organizations", nil, nil)
			if err != nil {
				return err
			}
			if client.LastMessage != "" && !s.Quiet {
				output.Info("%s", client.LastMessage)
			}
			if err := output.Emit(started, s.Output, s.Quiet); err != nil {
				return err
			}
			if !waitDone {
				return nil
			}
			// 与生成层的 library.scan 共用同一套等待：整理进度挂在库详情上
			return wait.Long(client, wait.LongTask{
				ProgressPath:    "/libraries/" + libraryID,
				ProgressField:   "organize_progress",
				ProgressCommand: "mclaw library get " + libraryID,
			}, waitTimeout)
		})
}

func newLibraryReconcilePathsCommand() *cobra.Command {
	var oldRoot, newRoot string
	var dryRun bool
	cmd := &cobra.Command{
		Use:   "reconcile-paths <library_id>",
		Short: "修复旧根路径遗留台账（预览影响面 → --yes 执行）",
		Long: `收口容器挂载前缀变更后遗留的旧路径台账。

正式执行会以持久化扫描作业重新盘点目标根；只会合并、标记或删除
library_file 台账记录，绝不会删除任何媒体文件。`,
		Args: cobra.ExactArgs(1),
	}
	cmd.Flags().StringVar(&oldRoot, "old-root", "", "已移除的旧根路径")
	cmd.Flags().StringVar(&newRoot, "new-root", "", "当前媒体库配置中的目标根路径")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "只输出修复预览，不扫描、不修改台账")
	_ = cmd.MarkFlagRequired("old-root")
	_ = cmd.MarkFlagRequired("new-root")

	return withOverrides(cmd, []string{"old-root", "new-root", "dry-run"},
		func(s *Settings, _ *cobra.Command, args []string) error {
			libraryID, err := parseLibraryID(args[0])
			if err != nil {
				return err
			}
			client, err := s.NewAPI()
			if err != nil {
				return err
			}
			base := "/libraries/" + libraryID
			body := map[string]any{"old_root": oldRoot, "new_root": newRoot}
			raw, err := client.Request("POST", base+"/path-reconciliation-preview", nil, body)
			if err != nil {
				return err
			}
			preview := jsonval.Object(raw)
			output.Info("路径迁移预览：同相对路径 %s，可安全合并 %s，将标记缺失 %s，身份冲突 %d，磁盘删除 0",
				plainOrZero(preview.Get("same_path_candidates")), plainOrZero(preview.Get("safe_merges")),
				plainOrZero(preview.Get("marked_missing")), len(jsonval.Array(preview.Get("conflicts"))))
			if dryRun {
				return output.Emit(preview, s.Output, s.Quiet)
			}
			if !s.Yes {
				if err := output.Emit(preview, s.Output, s.Quiet); err != nil {
					return err
				}
				return clierr.Newf(clierr.NeedConfirm, "路径迁移修复会修改旧路径台账，需要确认").
					WithHint("核对预览后重跑并加 --yes；只看预览用 --dry-run")
			}
			started, err := client.Request("POST", base+"/path-reconciliations", nil, body)
			if err != nil {
				return err
			}
			if client.LastMessage != "" && !s.Quiet {
				output.Info("%s", client.LastMessage)
			}
			return output.Emit(started, s.Output, s.Quiet)
		})
}

// parseLibraryID 校验并回传库 id 的字符串形态（要拼进 URL）。
func parseLibraryID(raw string) (string, error) {
	if _, err := strconv.Atoi(raw); err != nil {
		return "", clierr.Usagef("媒体库 id 必须是整数，收到 %q", raw).
			WithHint("用 mclaw library list 查看可用的库 id")
	}
	return raw, nil
}

// plainOrZero 渲染计数字段；服务端没给这个字段时显示 0 而不是空白。
func plainOrZero(value any) string {
	if value == nil {
		return "0"
	}
	return jsonval.Plain(value)
}
